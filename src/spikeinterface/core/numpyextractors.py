import numpy as np
from spikeinterface.core import (
    BaseRecording,
    BaseSorting,
    BaseRecordingSegment,
    BaseSortingSegment,
    BaseEvent,
    BaseEventSegment,
    BaseSnippets,
    BaseSnippetsSegment,
)
from .basesorting import minimum_spike_dtype
from .core_tools import make_shared_array

from multiprocessing.shared_memory import SharedMemory

from typing import List, Union


class NumpyRecording(BaseRecording):
    """
    In memory recording.
    Contrary to previous version this class does not handle npy files.

    Parameters
    ----------
    traces_list:  list of array or array (if mono segment)
        The traces to instantiate a mono or multisegment Recording
    sampling_frequency: float
        The sampling frequency in Hz
    t_starts: None or list of float
        Times in seconds of the first sample for each segment
    channel_ids: list
        An optional list of channel_ids. If None, linear channels are assumed
    """

    extractor_name = "Numpy"
    mode = "memory"
    name = "numpy"

    def __init__(self, traces_list, sampling_frequency, t_starts=None, channel_ids=None):
        if isinstance(traces_list, list):
            all_elements_are_list = all(isinstance(e, list) for e in traces_list)
            if all_elements_are_list:
                traces_list = [np.array(trace) for trace in traces_list]
            assert all(
                isinstance(e, np.ndarray) for e in traces_list
            ), f"must give a list of numpy array but gave {traces_list[0]}"
        else:
            assert isinstance(traces_list, np.ndarray), "must give a list of numpy array"
            traces_list = [traces_list]

        dtype = traces_list[0].dtype
        assert all(dtype == trace.dtype for trace in traces_list)

        if channel_ids is None:
            channel_ids = np.arange(traces_list[0].shape[1])
        else:
            channel_ids = np.asarray(channel_ids)
            assert channel_ids.size == traces_list[0].shape[1]
        BaseRecording.__init__(self, sampling_frequency, channel_ids, dtype)

        if t_starts is not None:
            assert len(t_starts) == len(traces_list), "t_starts must be a list of same size than traces_list"
            t_starts = [float(t_start) for t_start in t_starts]

        self._is_json_serializable = False

        for i, traces in enumerate(traces_list):
            if t_starts is None:
                t_start = None
            else:
                t_start = t_starts[i]
            rec_segment = NumpyRecordingSegment(traces, sampling_frequency, t_start)
            self.add_recording_segment(rec_segment)

        self._kwargs = {
            "traces_list": traces_list,
            "t_starts": t_starts,
            "sampling_frequency": sampling_frequency,
        }


class NumpyRecordingSegment(BaseRecordingSegment):
    def __init__(self, traces, sampling_frequency, t_start):
        BaseRecordingSegment.__init__(self, sampling_frequency=sampling_frequency, t_start=t_start)
        self._traces = traces
        self.num_samples = traces.shape[0]

    def get_num_samples(self):
        return self.num_samples

    def get_traces(self, start_frame, end_frame, channel_indices):
        traces = self._traces[start_frame:end_frame, :]
        if channel_indices is not None:
            traces = traces[:, channel_indices]

        return traces


class NumpySorting(BaseSorting):
    """
    In memory sorting object.
    The internal representation is always done with a long "spike vector".


    But we have convenient class methods to instantiate from:
      * other sorting object: `NumpySorting.from_sorting()`
      * from time+labels: `NumpySorting.from_times_labels()`
      * from dict of list: `NumpySorting.from_unit_dict()`
      * from neo: `NumpySorting.from_neo_spiketrain_list()`

    Parameters
    ----------
    spikes:  numpy.array
        A numpy vector, the one given by Sorting.to_spike_vector().
    sampling_frequency: float
        The sampling frequency in Hz
    channel_ids: list
        A list of unit_ids.
    """

    name = "numpy"

    def __init__(self, spikes, sampling_frequency, unit_ids):
        """ """
        BaseSorting.__init__(self, sampling_frequency, unit_ids)

        self._is_dumpable = True
        self._is_json_serializable = False

        if spikes.size == 0:
            nseg = 1
        else:
            nseg = spikes[-1]["segment_index"] + 1

        for segment_index in range(nseg):
            self.add_sorting_segment(NumpySortingSegment(spikes, segment_index, unit_ids))

        # important trick : the cache is already spikes vector
        self._cached_spike_vector = spikes

        self._kwargs = dict(spikes=spikes, sampling_frequency=sampling_frequency, unit_ids=unit_ids)

    @staticmethod
    def from_sorting(source_sorting: BaseSorting, with_metadata=False) -> "NumpySorting":
        """
        Create a numpy sorting from another sorting extractor
        """

        sorting = NumpySorting(
            source_sorting.to_spike_vector(), source_sorting.get_sampling_frequency(), source_sorting.unit_ids
        )
        if with_metadata:
            sorting.copy_metadata(source_sorting)
        return sorting

    @staticmethod
    def from_times_labels(times_list, labels_list, sampling_frequency, unit_ids=None) -> "NumpySorting":
        """
        Construct NumpySorting extractor from:
          * an array of spike times (in frames)
          * an array of spike labels and adds all the
        In case of multisegment, it is a list of array.

        Parameters
        ----------
        times_list: list of array (or array)
            An array of spike times (in frames).
        labels_list: list of array (or array)
            An array of spike labels corresponding to the given times.
        unit_ids: (None by default) the explicit list of unit_ids that should be extracted from labels_list
            If None, then it will be np.unique(labels_list)
        """

        if isinstance(times_list, np.ndarray):
            assert isinstance(labels_list, np.ndarray)
            times_list = [times_list]
            labels_list = [labels_list]

        times_list = [np.asarray(e) for e in times_list]
        labels_list = [np.asarray(e) for e in labels_list]

        nseg = len(times_list)

        if unit_ids is None:
            unit_ids = np.unique(np.concatenate([np.unique(labels_list[i]) for i in range(nseg)]))

        spikes = []
        for i in range(nseg):
            times, labels = times_list[i], labels_list[i]
            unit_index = np.zeros(labels.size, dtype="int64")
            for u, unit_id in enumerate(unit_ids):
                unit_index[labels == unit_id] = u
            spikes_in_seg = np.zeros(len(times), dtype=minimum_spike_dtype)
            spikes_in_seg["sample_index"] = times
            spikes_in_seg["unit_index"] = unit_index
            spikes_in_seg["segment_index"] = i
            order = np.argsort(times)
            spikes_in_seg = spikes_in_seg[order]
            spikes.append(spikes_in_seg)
        spikes = np.concatenate(spikes)

        sorting = NumpySorting(spikes, sampling_frequency, unit_ids)

        return sorting

    @staticmethod
    def from_unit_dict(units_dict_list, sampling_frequency) -> "NumpySorting":
        """
        Construct NumpySorting from a list of dict.
        The list length is the segment count.
        Each dict have unit_ids as keys and spike times as values.

        Parameters
        ----------
        dict_list: list of dict
        """
        if isinstance(units_dict_list, dict):
            units_dict_list = [units_dict_list]

        unit_ids = list(units_dict_list[0].keys())

        nseg = len(units_dict_list)
        spikes = []
        for seg_index in range(nseg):
            units_dict = units_dict_list[seg_index]

            sample_indices = []
            unit_indices = []
            for u, unit_id in enumerate(unit_ids):
                spike_times = units_dict[unit_id]
                sample_indices.append(spike_times)

                unit_indices.append(np.full(spike_times.size, u, dtype="int64"))
            if len(sample_indices) > 0:
                sample_indices = np.concatenate(sample_indices)
                unit_indices = np.concatenate(unit_indices)

                order = np.argsort(sample_indices)
                sample_indices = sample_indices[order]
                unit_indices = unit_indices[order]

            spikes_in_seg = np.zeros(len(sample_indices), dtype=minimum_spike_dtype)
            spikes_in_seg["sample_index"] = sample_indices
            spikes_in_seg["unit_index"] = unit_indices
            spikes_in_seg["segment_index"] = seg_index
            spikes.append(spikes_in_seg)
        spikes = np.concatenate(spikes)

        sorting = NumpySorting(spikes, sampling_frequency, unit_ids)

        # Trick : populate the cache with dict that already exists
        sorting._cached_spike_trains = {seg_ind: d for seg_ind, d in enumerate(units_dict_list)}

        return sorting

    @staticmethod
    def from_neo_spiketrain_list(neo_spiketrains, sampling_frequency, unit_ids=None) -> "NumpySorting":
        """
        Construct a NumpySorting with a neo spiketrain list.

        If this is a list of list, it is multi segment.

        Parameters
        ----------

        """
        import neo

        assert isinstance(neo_spiketrains, list)

        if isinstance(neo_spiketrains[0], list):
            # multi segment
            assert isinstance(neo_spiketrains[0][0], neo.SpikeTrain)
        elif isinstance(neo_spiketrains[0], neo.SpikeTrain):
            # unique segment
            neo_spiketrains = [neo_spiketrains]

        nseg = len(neo_spiketrains)

        if unit_ids is None:
            unit_ids = np.arange(len(neo_spiketrains[0]), dtype="int64")

        units_dict_list = []
        for seg_index in range(nseg):
            units_dict = {}
            for u, unit_id in enumerate(unit_ids):
                st = neo_spiketrains[seg_index][u]
                units_dict[unit_id] = (st.rescale("s").magnitude * sampling_frequency).astype("int64")
            units_dict_list.append(units_dict)

        sorting = NumpySorting.from_unit_dict(units_dict_list, sampling_frequency)

        return sorting

    @staticmethod
    def from_peaks(peaks, sampling_frequency, unit_ids=None) -> "NumpySorting":
        """
        Construct a sorting from peaks returned by 'detect_peaks()' function.
        The unit ids correspond to the recording channel ids and spike trains are the
        detected spikes for each channel.

        Parameters
        ----------
        peaks : structured np.array
            Peaks array as returned by the 'detect_peaks()' function
        sampling_frequency : float
            the sampling frequency in Hz

        Returns
        -------
        sorting
            The NumpySorting object
        """
        spikes = np.zeros(peaks.size, dtype=minimum_spike_dtype)
        spikes["sample_index"] = peaks["sample_index"]
        spikes["unit_index"] = peaks["channel_index"]
        spikes["segment_index"] = peaks["segment_index"]

        if unit_ids is None:
            unit_ids = np.unique(peaks["channel_index"])

        sorting = NumpySorting(spikes, sampling_frequency, unit_ids)

        return sorting


class NumpySortingSegment(BaseSortingSegment):
    def __init__(self, spikes, segment_index, unit_ids):
        BaseSortingSegment.__init__(self)
        self.spikes = spikes
        self.segment_index = segment_index
        self.unit_ids = list(unit_ids)
        self.spikes_in_seg = None

    def get_unit_spike_train(self, unit_id, start_frame, end_frame):
        if self.spikes_in_seg is None:
            # the slicing of segment is done only once the first time
            # this fasten the constructor a lot
            s0 = np.searchsorted(self.spikes["segment_index"], self.segment_index, side="left")
            s1 = np.searchsorted(self.spikes["segment_index"], self.segment_index + 1, side="left")
            self.spikes_in_seg = self.spikes[s0:s1]

        unit_index = self.unit_ids.index(unit_id)
        times = self.spikes_in_seg[self.spikes_in_seg["unit_index"] == unit_index]["sample_index"]

        if start_frame is not None:
            times = times[times >= start_frame]
        if end_frame is not None:
            times = times[times < end_frame]
        return times


class SharedMemorySorting(BaseSorting):
    def __init__(self, shm_name, shape, sampling_frequency, unit_ids, dtype=minimum_spike_dtype, main_shm_owner=True):
        assert len(shape) == 1
        assert shape[0] > 0, "SharedMemorySorting only supported with no empty sorting"

        BaseSorting.__init__(self, sampling_frequency, unit_ids)
        self._is_dumpable = True
        self._is_json_serializable = False

        self.shm = SharedMemory(shm_name, create=False)
        self.shm_spikes = np.ndarray(shape=shape, dtype=dtype, buffer=self.shm.buf)

        nseg = self.shm_spikes[-1]["segment_index"] + 1
        for segment_index in range(nseg):
            self.add_sorting_segment(NumpySortingSegment(self.shm_spikes, segment_index, unit_ids))

        # important trick : the cache is already spikes vector
        self._cached_spike_vector = self.shm_spikes

        # this is very important for the shm.unlink()
        # only the main instance need to call it
        # all other instances that are loaded from dict are not the main owner
        self.main_shm_owner = main_shm_owner

        self._kwargs = dict(
            shm_name=shm_name,
            shape=shape,
            sampling_frequency=sampling_frequency,
            unit_ids=unit_ids,
            # this ensure that all dump/load will not be main shm owner
            main_shm_owner=False,
        )

    def __del__(self):
        self.shm.close()
        if self.main_shm_owner:
            self.shm.unlink()

    @staticmethod
    def from_sorting(source_sorting):
        spikes = source_sorting.to_spike_vector()
        shm_spikes, shm = make_shared_array(spikes.shape, spikes.dtype)
        shm_spikes[:] = spikes
        sorting = SharedMemorySorting(
            shm.name,
            spikes.shape,
            source_sorting.get_sampling_frequency(),
            source_sorting.unit_ids,
            dtype=spikes.dtype,
            main_shm_owner=True,
        )
        shm.close()
        return sorting


class NumpyEvent(BaseEvent):
    def __init__(self, channel_ids, structured_dtype):
        BaseEvent.__init__(self, channel_ids, structured_dtype)

    @staticmethod
    def from_dict(event_dict_list):
        """
        Constructs NumpyEvent from a dictionary

        Parameters
        ----------
        event_dict_list : list
            List of dictionaries with channel_ids as keys and event data as values.
            Each list element corresponds to an event segment.
            If values have a simple dtype, they are considered the timestamps.
            If values have a structured dtype, the have to contain a "times" or "timestamps"
            field.

        Returns
        -------
        NumpyEvent
            The Event object
        """
        if isinstance(event_dict_list, dict):
            event_dict_list = [event_dict_list]

        channel_ids = list(event_dict_list[0].keys())

        structured_dtype = {}
        for chan_id in channel_ids:
            values = np.array(event_dict_list[0][chan_id])
            structured_dtype[chan_id] = values.dtype

        event = NumpyEvent(channel_ids, structured_dtype)
        for i, event_dict in enumerate(event_dict_list):
            event.add_event_segment(NumpyEventSegment(event_dict))

        return event


class NumpyEventSegment(BaseEventSegment):
    def __init__(self, event_dict):
        BaseEventSegment.__init__(self)

        self._event_dict = event_dict

    def get_events(self, channel_id, start_time, end_time):
        events = self._event_dict[channel_id]
        if events.dtype.fields is None:
            times = events
            # no structured dtype, we assume "times"
        else:
            if "time" in events.dtype.names:
                times = events["time"]
            else:
                times = events["timestamp"]
        if start_time is not None:
            events = events[times >= start_time]
        if end_time is not None:
            events = events[times <= end_time]
        return events


class NumpySnippets(BaseSnippets):
    """
    In memory recording.
    Contrary to previous version this class does not handle npy files.

    Parameters
    ----------
    snippets_list:  list of array or array (if mono segment)
        The snippets to instantiate a mono or multisegment basesnippet
    spikesframes_list: list of array or array (if mono segment)
        Frame of each snippet
    sampling_frequency: float
        The sampling frequency in Hz

    channel_ids: list
        An optional list of channel_ids. If None, linear channels are assumed
    """

    def __init__(self, snippets_list, spikesframes_list, sampling_frequency, nbefore=None, channel_ids=None):
        if isinstance(snippets_list, list):
            assert all(isinstance(e, np.ndarray) for e in snippets_list), "must give a list of numpy array"
        else:
            assert isinstance(snippets_list, np.ndarray), "must give a list of numpy array"
            snippets_list = [snippets_list]
        if isinstance(spikesframes_list, list):
            assert all(isinstance(e, np.ndarray) for e in spikesframes_list), "must give a list of numpy array"
        else:
            assert isinstance(spikesframes_list, np.ndarray), "must give a list of numpy array"
            spikesframes_list = [spikesframes_list]

        dtype = snippets_list[0].dtype
        assert all(dtype == ts.dtype for ts in snippets_list)

        if channel_ids is None:
            channel_ids = np.arange(snippets_list[0].shape[2])
        else:
            channel_ids = np.asarray(channel_ids)
            assert channel_ids.size == snippets_list[0].shape[2]
        BaseSnippets.__init__(
            self,
            sampling_frequency,
            nbefore=nbefore,
            snippet_len=snippets_list[0].shape[1],
            channel_ids=channel_ids,
            dtype=dtype,
        )

        self._is_dumpable = False
        self._is_json_serializable = False

        for snippets, spikesframes in zip(snippets_list, spikesframes_list):
            snp_segment = NumpySnippetsSegment(snippets, spikesframes)
            self.add_snippets_segment(snp_segment)

        self._kwargs = {
            "snippets_list": snippets_list,
            "spikesframes_list": spikesframes_list,
            "nbefore": nbefore,
            "sampling_frequency": sampling_frequency,
            "channel_ids": channel_ids,
        }


class NumpySnippetsSegment(BaseSnippetsSegment):
    def __init__(self, snippets, spikesframes):
        BaseSnippetsSegment.__init__(self)
        self._snippets = snippets
        self._spikestimes = spikesframes

    def get_snippets(
        self,
        indices,
        channel_indices: Union[List, None] = None,
    ) -> np.ndarray:
        """
        Return the snippets, optionally for a subset of samples and/or channels

        Parameters
        ----------
        indexes: (Union[int, None], optional)
            start sample index, or zero if None. Defaults to None.
        end_frame: (Union[int, None], optional)
            end_sample, or number of samples if None. Defaults to None.
        channel_indices: (Union[List, None], optional)
            Indices of channels to return, or all channels if None. Defaults to None.

        Returns
        -------
        snippets: np.ndarray
            Array of snippets, num_snippets x num_samples x num_channels
        """
        if indices is None:
            return self._snippets[:, :, channel_indices]
        return self._snippets[indices, :, channel_indices]

    def get_num_snippets(self):
        return self._spikestimes.shape[0]

    def frames_to_indices(self, start_frame: Union[int, None] = None, end_frame: Union[int, None] = None):
        """
        Return the slice of snippets

        Parameters
        ----------
        start_frame: (Union[int, None], optional)
            start sample index, or zero if None. Defaults to None.
        end_frame: (Union[int, None], optional)
            end_sample, or number of samples if None. Defaults to None.

        Returns
        -------
        snippets: slice
            slice of selected snippets
        """
        # must be implemented in subclass
        if start_frame is None:
            init = 0
        else:
            init = np.searchsorted(self._spikestimes, start_frame, side="left")
        if end_frame is None:
            endi = self._spikestimes.shape[0]
        else:
            endi = np.searchsorted(self._spikestimes, end_frame, side="left")
        return slice(init, endi, 1)

    def get_frames(self, indices=None):
        """Returns the frames of the snippets in this segment

        Returns:
            SampleIndex: Number of samples in the segment
        """
        if indices is None:
            return self._spikestimes
        raise self._spikestimes[indices]
