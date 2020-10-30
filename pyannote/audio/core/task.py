# MIT License
#
# Copyright (c) 2020 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Text, Tuple, Union

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import torch.optim
from torch.utils.data import DataLoader, IterableDataset

from pyannote.audio.core.io import AudioFile
from pyannote.core import Segment, SlidingWindow
from pyannote.database import Protocol

if TYPE_CHECKING:
    from pyannote.audio.core.model import Model


# Type of machine learning problem
class Problem(Enum):
    MONO_LABEL_CLASSIFICATION = 1
    MULTI_LABEL_CLASSIFICATION = 2
    REPRESENTATION = 3
    REGRESSION = 4
    # any other we could think of?


# A task takes an audio chunk as input and returns
# either a temporal sequence of predictions
# or just one prediction for the whole audio chunk
class Scale(Enum):
    FRAME = 1  # model outputs a sequence of frames
    CHUNK = 2  # model outputs just one vector for the whole chunk


@dataclass
class TaskSpecification:
    problem: Problem
    scale: Scale

    # chunk duration in seconds.
    # use None for variable-length chunks
    duration: Optional[float] = None

    # (for classification tasks only) list of classes
    classes: Optional[List[Text]] = None


class Task(pl.LightningDataModule):
    """Base task class

    A task is the combination of a "problem" and a "dataset".
    For example, here are a few tasks:
    - voice activity detection on the AMI corpus
    - speaker embedding on the VoxCeleb corpus
    - end-to-end speaker diarization on the VoxConverse corpus

    A task is expected to be solved by a "model" that takes an
    audio chunk as input and returns the solution. Hence, the
    task is in charge of generating (input, expected_output)
    samples used for training the model.

    Parameters
    ----------
    protocol : Protocol
        pyannote.database protocol
    duration : float, optional
        Chunks duration. Defaults to variable duration (None).
    batch_size : int, optional
        Number of training samples per batch.
    num_workers : int, optional
        Number of workers used for generating training samples.
    """

    def __init__(
        self,
        protocol: Protocol,
        duration: float = None,
        batch_size: int = None,
        num_workers: int = 1,
    ):
        super().__init__()

        # dataset
        self.protocol = protocol

        # batching
        self.duration = duration
        self.batch_size = batch_size
        self.num_workers = num_workers

    def prepare_data(self):
        # this is where we might end up downloading datasets
        # and transform them so that they are ready to be used
        # with pyannote.database. but for now, the API assume
        # that we directly provide a pyannote.database.Protocol.
        pass

    def prepare_chunk(
        self,
        file: AudioFile,
        chunk: Segment,
        duration: float = None,
        return_y: bool = False,
        labels: List[Text] = None,
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray, List[Text]]]:
        """Extract audio chunk and corresponding frame-wise labels

        Parameters
        ----------
        file : AudioFile
            Audio file.
        chunk : Segment
            Audio chunk.
        duration : float, optional
            Fix chunk duration to avoid rounding errors. Defaults to self.duration
        return_y : bool, optional
            Set to True to return frame-wise labels.
        labels : list of str, optional
            Ordered labels such that y[:, k] corresponds to activity of labels[k].
            Defaults to file['annotation'].crop(chunk).labels()

        Returns
        -------
        X : np.ndarray
            Audio chunk as (num_samples, num_channels) array.
        y : np.ndarray, optional
            Frame-wise labels (if return_y is True) as (num_frames, num_labels) array.
        labels : list of str, optional
            Labels (if return_y is True), index-aligned with y.
        """

        X, _ = self.audio.crop(
            file,
            chunk,
            mode="center",
            fixed=self.duration if duration is None else duration,
        )
        if not return_y:
            return X

        num_frames, _ = self.model_introspection(X.shape[0])
        annotation = file["annotation"].crop(chunk)
        labels = annotation.labels() if labels is None else labels

        y = np.zeros((num_frames, len(labels)), dtype=np.int8)
        frames = SlidingWindow(
            start=chunk.start,
            duration=self.duration / num_frames,
            step=self.duration / num_frames,
        )
        for label in annotation.labels():
            try:
                k = labels.index(label)
            except ValueError:
                raise ValueError(
                    f"File {file['uri']} contains unexpected label '{label}'."
                )

            segments = annotation.label_timeline(label)
            for start, stop in frames.crop(segments, mode="center", return_ranges=True):
                y[start:stop, k] += 1

        # handle corner case when the same label is active more than once
        y = np.minimum(y, 1, out=y)

        return X, y, labels

    def train__iter__(self):
        # will become train_dataset.__iter__ method
        msg = f"Missing '{self.__class__.__name__}.train__iter__' method."
        raise NotImplementedError(msg)

    def train__len__(self):
        # will become train_dataset.__len__ method
        msg = f"Missing '{self.__class__.__name__}.train__len__' method."
        raise NotImplementedError(msg)

    def train_dataloader(self) -> DataLoader:
        # build train IterableDataset subclass programmatically
        dataset = type(
            "TrainDataset",
            (IterableDataset,),
            {"__iter__": self.train__iter__, "__len__": self.train__len__},
        )

        return DataLoader(
            dataset(),
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            drop_last=True,
        )

    @property
    def example_input_duration(self) -> float:
        return 2.0 if self.duration is None else self.duration

    def example_input_array(self):
        # this method is called in Model.introspect where it is used
        # to automagically infer the temporal resolution of the
        # model output, and hence allow the dataloader to shape
        # its targets correctly.

        # since we plan to have the feature extraction step done
        # on GPU as part of the model, the example input array is
        # basically always a chunk of audio

        if self.audio.mono:
            num_channels = 1
        else:
            msg = "Only 'mono' audio is supported."
            raise NotImplementedError(msg)

        return torch.randn(
            (
                self.batch_size,
                int(self.audio.sample_rate * self.example_input_duration),
                num_channels,
            )
        )

    # default training_step provided for convenience
    # can obviously be overriden for each task
    def training_step(self, model: "Model", batch, batch_idx: int):
        X, y = batch["X"], batch["y"]
        if self.specifications.problem == Problem.MONO_LABEL_CLASSIFICATION:
            loss = F.nll_loss(
                model(X).view(-1, len(self.specifications.classes)), y.view(-1)
            )

        elif self.specifications.problem == Problem.MULTI_LABEL_CLASSIFICATION:
            loss = F.binary_cross_entropy(model(X), y.float())

        else:
            msg = "TODO: implement for other types of problems"
            raise NotImplementedError(msg)

        model.log("train_loss", loss)
        return loss

    # default configure_optimizers provided for convenience
    # can obviously be overriden for each task
    def configure_optimizers(self, model: "Model"):
        # for tasks such as SpeakerEmbedding,
        # other parameters should be added here
        return torch.optim.Adam(model.parameters(), lr=1e-3)