"""
The reader is part of the new database concept 2017.

The task of the reader is to take a database JSON and an dataset identifier as
an input and load all meta data for each observation with corresponding
numpy arrays for each time signal (non stacked).

An example ID often stands for utterance ID. In case of speaker mixtures,
it replaces mixture ID. Also, in case of event detection utterance ID is not
very adequate.

The JSON file is specified as follows:

datasets:
    <dataset name 0>
        <unique example id 1> (unique within a dataset)
            audio_path:
                speech_source:
                    <path to speech of speaker 0>
                    <path to speech of speaker 1>
                observation:
                    blue_array: (a list, since there are no missing channels)
                        <path to observation of blue_array and channel 0>
                        <path to observation of blue_array and channel 0>
                        ...
                    red_array: (special case for missing channels)
                        c0: <path to observation of red_array and channel 0>
                        c99: <path to observation of red_array and channel 99>
                        ...
                speech_image:
                    ...
            speaker_id:
                <speaker_id for speaker 0>
                ...
            gender:
                <m/f>
                ...
            ...

Make sure, all keys are natsorted in the JSON file.

Make sure, the names are not redundant and it is clear, which is train, dev and
test set. The names should be as close as possible to the original database
names.

An observation/ example has information according to the keys file.

If a database does not have different arrays, the array dimension can be
omitted. Same holds true for the channel axis or the speaker axis.

The different axis have to be natsorted, when they are converted to numpy
arrays. Skipping numbers (i.e. c0, c99) is database specific and is not handled
by a generic implementation.

If audio paths are a list, they will be stacked to a numpy array. If it is a
dictionary, it will become a dictionary of numpy arrays.

If the example IDs are not unique in the original database, the example IDs
are made unique by prefixing them with the dataset name of the original
database, i.e. dt_simu_c0123.
"""
import logging
import numbers
from collections import ChainMap
from copy import deepcopy
from pathlib import Path

import numpy as np

from nt import kaldi
from nt.database import keys
from nt.io.audioread import audioread

LOG = logging.getLogger('Database')


class BaseIterator:
    def __call__(self):
        return self.__iter__()

    def __iter__(self):
        raise NotImplementedError(
            f'__iter__ is not implemented for {self.__class__}.\n'
            f'self: \n{repr(self)}'
        )

    def __len__(self):
        raise NotImplementedError(
            f'__len__ is not implemented for {self.__class__}.\n'
            f'self: \n{repr(self)}'
        )

    def __getitem__(self, item):
        if isinstance(item, (slice, tuple, list)):
            return SliceIterator(item, self)
        raise NotImplementedError(
            f'__getitem__ is not implemented for {self.__class__}[{item}],\n'
            f'where type({item}) == {type(item)} '
            f'self: \n{repr(self)}'
        )

    def keys(self):
        raise NotImplementedError(
            f'keys is not implemented for {self.__class__}.\n'
            f'self: \n{repr(self)}'
        )

    def map(self, map_fn):
        """
        :param map_fn: function to transform an example dict. Takes an example
            dict as provided by this iterator and returns a transformed
            example dict, e.g. read and adss the observed audio signals.
        :return: MapIterator returning mapped examples. This can e.g. be
        used to read and add audio to the example dict (see read_audio method).

        Note:
            map_fn can do inplace transformations without using copy.
            The ExampleIterator makes a deepcopy of each example and prevents a
            modification of the root example.
        """
        return MapIterator(map_fn, self)

    def filter(self, filter_fn):
        """
        Filtering examples. If possible this method should be called before
        applying expensive map functions.
        :param filter_fn: function to filter examples, takes example as input
            and returns True if example should be kept, else False.
        :return: FilterIterator iterating over filtered examples.
        """
        return FilterIterator(filter_fn, self)

    def concatenate(self, *others):
        """
        Concatenate this iterator with others. keys need to be unambiguous.
        :param others: list of other iterators to be concatenated
        :return: ExamplesIterator iterating over all examples.
        """
        return ConcatenateIterator(self, *others)

    def shuffle(self, reshuffle=False):
        """
        Shuffle this iterator.
        :param reshuffle:
            If True, shuffle on each iteration, but disable indexing.
            If False, single shuffle, but support indexing.
        :return:
        """
        # Should reshuffle default be True or False
        if reshuffle is True:
            return ReShuffleIterator(self)
        elif reshuffle is False:
            return ShuffleIterator(self)
        else:
            raise ValueError(reshuffle, self)

    def __str__(self):
        return f'{self.__class__.__name__}()'

    def __repr__(self):
        r = ''
        if hasattr(self, 'input_dataset'):
            r += repr(self.input_dataset) + '\n '
        return r + str(self)


class ExamplesIterator(BaseIterator):
    """
    Iterator to iterate over a list of examples with each example being a dict
    according to the json structure as outline in the top of this file.
    """

    def __init__(self, examples, name=None):
        assert isinstance(examples, dict)
        self.examples = examples
        self.name = name

    def __str__(self):
        if self.name is None:
            return f'{self.__class__.__name__}(len={len(self)})'
        else:
            return f'{self.__class__.__name__}' \
                   f'(name={self.name}, len={len(self)})'

    def keys(self):
        return list(self.examples.keys())

    def __iter__(self):
        for k in self.keys():
            yield self[k]

    def __getitem__(self, item):
        if isinstance(item, str):
            if item in self.keys():
                key = item
            else:
                raise IndexError(item)
        elif isinstance(item, numbers.Integral):
            key = self.keys()[item]
        else:
            return super().__getitem__(item)
        example = deepcopy(self.examples[key])
        example[keys.EXAMPLE_ID] = key
        return example

    def __len__(self):
        return len(self.examples)


class MapIterator(BaseIterator):
    """
    Iterator that iterates over an input_iterator and applies a transformation
    map_function to each element.

    .. note: This Iterator makes a (deep)copy of the example before applying the
        function.


    """

    def __init__(self, map_function, input_iterator):
        """

        :param map_function: function that transforms an element of
            input_iterator. Use deepcopy within the map_function if necessary.
        :param input_iterator: any iterator (e.g. ExampleIterator)
        """
        assert callable(map_function), map_function
        self.map_function = map_function
        self.input_iterator = input_iterator

    def __str__(self):
        return f'{self.__class__.__name__}({self.map_function})'

    def __len__(self):
        return len(self.input_iterator)

    def __iter__(self):
        for example in self.input_iterator:
            yield self.map_function(example)

    def keys(self):
        return self.input_iterator.keys()

    def __getitem__(self, item):
        if isinstance(item, (str, numbers.Integral)):
            return self.map_function(self.input_iterator[item])
        else:
            return super().__getitem__(item)


class ShuffleIterator(BaseIterator):
    """
    Iterator that shuffles the input_iterator. Assumes, that the input_iterator
    has a length.
    Note:
        This Iterator supports indexing, but does not reshuffle each iteration.
    """

    def __init__(self, input_iterator):
        self.permutation = np.arange(len(input_iterator))
        np.random.shuffle(self.permutation)
        self.input_iterator = input_iterator

    def __len__(self):
        return len(self.input_iterator)

    def keys(self):
        return self.input_iterator.keys()

    def __iter__(self):
        for idx in self.permutation:
            yield self.input_iterator[idx]

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.input_iterator[item]
        elif isinstance(item, numbers.Integral):
            return self.input_iterator[self.permutation[item]]
        else:
            return super().__getitem__(item)


class ReShuffleIterator(BaseIterator):
    """
    Iterator that shuffles the input_iterator. Assumes, that the input_iterator
    has a length.
    Note:
        This Iterator reshuffle each iteration, but does not support indexing.
    """

    def __init__(self, input_iterator):
        self.permutation = np.arange(len(input_iterator))
        self.input_iterator = input_iterator

    def __len__(self):
        return len(self.input_iterator)

    def keys(self):
        return self.input_iterator.keys()

    def __iter__(self):
        np.random.shuffle(self.permutation)
        for idx in self.permutation:
            yield self.input_iterator[idx]

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.input_iterator[item]
        elif isinstance(item, numbers.Integral):
            raise TypeError(item)
        else:
            return super().__getitem__(item)


class SliceIterator(BaseIterator):
    def __init__(self, slice, input_iterator):
        self._slice = slice
        self.slice = np.arange(len(input_iterator))[self._slice]
        self.input_iterator = input_iterator

    def keys(self):
        return self.input_iterator.keys()[self.slice]

    def __len__(self):
        return len(self.slice)

    def __str__(self):
        return f'{self.__class__.__name__}({self._slice})'

    def __iter__(self):
        for idx in self.slice:
            yield self.input_iterator[idx]

    def __getitem__(self, key):
        if isinstance(key, numbers.Integral):
            return self.input_iterator[self.slice[key]]
        elif isinstance(key, str):
            if key in self.keys():
                return self.input_iterator[key]
            else:
                raise IndexError(key)
        else:
            return super().__getitem__(key)


class FilterIterator(BaseIterator):
    """
    Iterator that iterates only over those elements of input_iterator that meet
    filter_function.
    """

    def __init__(self, filter_function, input_iterator):
        """

        :param filter_function: a function that takes an element of the input
            iterator and returns True if the element is valid else False.
        :param input_iterator: any iterator (e.g. ExampleIterator)
        """
        assert callable(filter_function), filter_function
        self.filter_function = filter_function
        self.input_iterator = input_iterator

    def __str__(self):
        return f'{self.__class__.__name__}({self.filter_function})'

    def __iter__(self):
        for example in self.input_iterator:
            if self.filter_function(example):
                yield example

    def __getitem__(self, key):
        assert isinstance(key, str), (
            f'key == {key}\n{self.__class__} does not support __getitem__ '
            f'for type(key) == {type(key)},\n'
            f'Only type str is allowed.\n'
            f'self:\n{repr(self)}'
        )
        ex = self.input_iterator[key]
        if not self.filter_function(ex):
            raise IndexError(key)
        return ex


class ConcatenateIterator(BaseIterator):
    """
    Iterates over all elements of all input_iterators.
    Best use is to concatenate cross validation or evaluation datasets.
    It does not work well with buffer based shuffle (i.e. in Tensorflow).
    """

    def __init__(self, *input_iterators):
        """
        :param input_iterators: list of iterators
        """
        self.input_iterators = input_iterators

    def __str__(self):
        return f'{self.__class__.__name__}({self.input_iterators})'

    def __repr__(self):
        return f'{self.__class__.__name__}({self.input_iterators})'

    def __iter__(self):
        for input_iterator in self.input_iterators:
            for example in input_iterator:
                yield example

    def __len__(self):
        return sum([len(i) for i in self.input_iterators])

    _keys = None

    def keys(self):
        if self._keys is None:
            self._keys = []
            for iterator in self.input_iterators:
                self._keys += list(iterator.keys())
            assert len(self._keys) == len(set(self._keys)), \
                'Keys are not unique. ' \
                'len(self._keys) = {len(self._keys)} != ' \
                '{len(set(self._keys))} = len(set(self._keys))'
        return self._keys

    _chain_map = None

    def __getitem__(self, item):
        if isinstance(item, numbers.Integral):
            for iterator in self.input_iterators:
                if len(iterator) <= item:
                    item -= len(iterator)
                else:
                    return iterator[item]
        elif isinstance(item, str):
            if self._chain_map is None:
                self.keys()  # test unique keys
                self._chain_map = ChainMap(*self.input_iterators)
            return self._chain_map[item]
        else:
            return super().__getitem__(item)


class MixIterator(BaseIterator):
    """
    Provide
    """

    def __init__(self, *input_iterators, p=None):
        """
        :param input_iterators:
        :param p: Probabilities for each iterator. Equal probability if None.
        """
        count = len(input_iterators)
        if p is None:
            self.p = np.full((count,), 1 / count)
        else:
            assert count == len(p), f'{count} != {len(p)}'

    def __iter__(self):
        raise NotImplementedError


def recursive_transform(func, dict_list_val, list2array=False):
    """
    Applies a function func to all leaf values in a dict or list or directly to
    a value. The hierarchy of dict_list_val is inherited. Lists are stacked
    to numpy arrays. This function can e.g. be used to recursively apply a
    transformation (e.g. audioread) to all audio paths in an example dict
    (see top of this file).
    :param func: a transformation function to be applied to the leaf values
    :param dict_list_val: dict list or value
    :param args: args for func
    :param kwargs: kwargs for func
    :param list2array:
    :return: dict, list or value with transformed elements
    """
    if isinstance(dict_list_val, dict):
        # Recursively call itself
        return {key: recursive_transform(func, val, list2array)
                for key, val in dict_list_val.items()}
    if isinstance(dict_list_val, (list, tuple)):
        # Recursively call itself
        l = [recursive_transform(func, val, list2array)
             for val in dict_list_val]
        if list2array:
            return np.array(l)
        return l
    else:
        # applies function to a leaf value which is not a dict or list
        return func(dict_list_val)


class AudioReader:
    def __init__(self, src_key='audio_path', dst_key='audio_data',
                 audio_keys='observation', read_fn=lambda x: audioread(x)[0]):
        """
        recursively read audio files and add audio
        signals to the example dict.
        :param src_key: key in an example dict where audio file paths can be
            found.
        :param dst_key: key to add the read audio to the example dict.
        :param audio_keys: str or list of subkeys that are relevant. This can be
            used to prevent unnecessary audioread.
        """
        self.src_key = src_key
        self.dst_key = dst_key
        if audio_keys is not None:
            self.audio_keys = to_list(audio_keys)
        else:
            self.audio_keys = None
        self._read_fn = read_fn

    def __call__(self, example):
        """
        :param example: example dict with src_key in it
        :return: example dict with audio data added
        """
        if self.audio_keys is not None:
            data = {
                audio_key: recursive_transform(
                    self._read_fn, example[self.src_key][audio_key],
                    list2array=True
                )
                for audio_key in self.audio_keys
            }
        else:
            data = recursive_transform(
                self._read_fn, example[self.src_key], list2array=True
            )

        if self.dst_key is not None:
            example[self.dst_key] = data
        else:
            example.update(data)
        return example


class IdFilter:
    def __init__(self, id_list):
        """
        A filter to filter example ids.
        :param id_list: list of valid ids, e.g. ids belonging to a specific
            dataset.
        """
        self.id_list = id_list

    def __call__(self, example):
        """
        :param example: example dict with example_id in it
        :return: True if example_id in id_list else False
        """
        return example[keys.EXAMPLE_ID] in self.id_list


def to_list(x):
    if isinstance(x, (list, tuple)):
        return x
    return [x]


class AlignmentReader:
    def __init__(
            self, alignment_path: Path = None, alignments: dict = None,
            example_id_map_fn=lambda x: x[keys.EXAMPLE_ID]):
        assert alignment_path is not None or alignments is not None, (
            'Either alignments or the path to the alignments must be specified'
        )
        self._ali_path = alignment_path
        self._alignments = alignments
        self._map_fn = example_id_map_fn

    def __call__(self, example):
        if self._alignments is None:
            self._alignments = \
                kaldi.alignment.import_alignment_data(self._ali_path)
            LOG.debug(
                f'Read {len(self._alignments)} alignments '
                f'from path {self._ali_path}'
            )
        try:
            example[keys.ALIGNMENT] = self._alignments[
                self._map_fn(example)
            ]
            example[keys.NUM_ALIGNMENT_FRAMES] = len(example[keys.ALIGNMENT])
        except KeyError:
            LOG.warning(
                f'No alignment found for example id {example[keys.EXAMPLE_ID]} '
                f'(mapped: {self._map_fn(example)}).'
            )
        return example


def remove_examples_without_alignment(example):
    valid_ali = keys.ALIGNMENT in example and len(example[keys.ALIGNMENT])
    if not valid_ali:
        LOG.warning(f'No alignment found for example\n{example}')
        return False
    if keys.NUM_SAMPLES in example:
        num_samples = example[keys.NUM_SAMPLES]
        if isinstance(num_samples, dict):
            num_samples = num_samples[keys.OBSERVATION]
    else:
        return True  # Only happens for Kaldi databases
    num_frames = (num_samples - 400 + 160) // 160
    num_frames_lfr = (num_frames + np.mod(-num_frames, 3)) // 3
    len_ali = len(example[keys.ALIGNMENT])
    valid_ali = (
        len_ali == num_frames or
        len_ali == num_frames_lfr
    )
    if not valid_ali:
        LOG.warning(
            f'Alignment has {len_ali} frames but the observation has '
            f'{num_frames} [{num_frames_lfr}] frames. Example was:\n{example}'
        )
        return False
    return True


class Word2Id:
    def __init__(self, word2id_fn):
        self._word2id_fn = word2id_fn

    def __call__(self, example):
        def _w2id(s):
            return np.array([self._word2id_fn(w) for w in s.split()], np.int32)

        for trans in [keys.TRANSCRIPTION, keys.KALDI_TRANSCRIPTION]:
            try:
                example[trans + '_ids'] = recursive_transform(
                    _w2id, example[trans]
                )
            except KeyError:
                pass
        return example