# Authors: Adam Li <adam2392@gmail.com>
#          Santeri Ruuskanen <santeriruuskanen@gmail.com>
#          Thomas S. Binns <t.s.binns@outlook.com>
#
# License: BSD (3-clause)

import numpy as np
import xarray as xr
from mne.epochs import BaseEpochs
from mne.parallel import parallel_func
from mne.time_frequency import (tfr_array_morlet, tfr_array_multitaper,
                                dpss_windows)
from mne.utils import (logger, verbose)

from ..base import (SpectralConnectivity, EpochSpectralConnectivity)
from .epochs import (_MICEst, _MIMEst, _GCEst, _GCTREst, _compute_freq_mask,
                     _check_rank_input)
from .smooth import _create_kernel, _smooth_spectra
from ..utils import check_indices, fill_doc


_multivariate_methods = ['mic', 'mim', 'gc', 'gc_tr']
_gc_methods = ['gc', 'gc_tr']


@verbose
@fill_doc
def spectral_connectivity_time(data, freqs, method='coh', average=False,
                               indices=None, sfreq=None, fmin=None,
                               fmax=None, fskip=0, faverage=False, sm_times=0,
                               sm_freqs=1, sm_kernel='hanning', padding=0,
                               mode='cwt_morlet', mt_bandwidth=None,
                               n_cycles=7, gc_n_lags=40, rank=None, decim=1,
                               n_jobs=1, verbose=None):
    r"""Compute time-frequency-domain connectivity measures.

    This function computes spectral connectivity over time from epoched data.
    The data may consist of a single epoch.

    The connectivity method(s) are specified using the ``method`` parameter.
    All methods are based on time-resolved estimates of the cross- and
    power spectral densities (CSD/PSD) Sxy and Sxx, Syy.

    Parameters
    ----------
    data : array_like, shape (n_epochs, n_signals, n_times) | Epochs
        The data from which to compute connectivity.
    freqs : array_like
        Array of frequencies of interest for time-frequency decomposition.
        Only the frequencies within the range specified by ``fmin`` and
        ``fmax`` are used.
    method : str | list of str
        Connectivity measure(s) to compute. These can be
        ``['coh', 'mic', 'mim', 'plv', 'ciplv', 'pli', 'wpli', 'gc',
        'gc_tr']``. These are:
        * 'coh'   : Coherence
        * 'mic'   : Maximised Imaginary part of Coherency (MIC)
        * 'mim'   : Multivariate Interaction Measure (MIM)
        * 'plv'   : Phase-Locking Value (PLV)
        * 'ciplv' : Corrected imaginary Phase-Locking Value
        * 'pli'   : Phase-Lag Index
        * 'wpli'  : Weighted Phase-Lag Index
        * 'gc'    : State-space Granger Causality (GC)
        * 'gc_tr' : State-space GC on time-reversed signals
        Multivariate methods (``['mic', 'mim', 'gc', 'gc_tr]``) cannot be
        called with the other methods.
    average : bool
        Average connectivity scores over epochs. If ``True``, output will be
        an instance of :class:`SpectralConnectivity`, otherwise
        :class:`EpochSpectralConnectivity`.
    indices : tuple of array_like | None
        Two arrays with indices of connections for which to compute
        connectivity. If a multivariate method is called, the indices are for a
        single connection between all seeds and all targets. If None, all
        connections are computed, unless a Granger causality method is called,
        in which case an error is raised.
    sfreq : float
        The sampling frequency. Required if data is not
        :class:`Epochs <mne.Epochs>`.
    fmin : float | tuple of float | None
        The lower frequency of interest. Multiple bands are defined using
        a tuple, e.g., ``(8., 20.)`` for two bands with 8 Hz and 20 Hz lower
        bounds. If `None`, the lowest frequency in ``freqs`` is used.
    fmax : float | tuple of float | None
        The upper frequency of interest. Multiple bands are defined using
        a tuple, e.g. ``(13., 30.)`` for two band with 13 Hz and 30 Hz upper
        bounds. If `None`, the highest frequency in ``freqs`` is used.
    fskip : int
        Omit every ``(fskip + 1)``-th frequency bin to decimate in frequency
        domain.
    faverage : bool
        Average connectivity scores for each frequency band. If `True`,
        the output ``freqs`` will be an array of the median frequencies of each
        band.
    sm_times : float
        Amount of time to consider for the temporal smoothing in seconds.
        If zero, no temporal smoothing is applied.
    sm_freqs : int
        Number of points for frequency smoothing. By default, 1 is used which
        is equivalent to no smoothing.
    sm_kernel : {'square', 'hanning'}
        Smoothing kernel type. Choose either 'square' or 'hanning'.
    padding : float
        Amount of time to consider as padding at the beginning and end of each
        epoch in seconds. See Notes for more information.
    mode : str
        Time-frequency decomposition method. Can be either: 'multitaper', or
        'cwt_morlet'. See :func:`mne.time_frequency.tfr_array_multitaper` and
        :func:`mne.time_frequency.tfr_array_morlet` for reference.
    mt_bandwidth : float | None
        Product between the temporal window length (in seconds) and the full
        frequency bandwidth (in Hz). This product can be seen as the surface
        of the window on the time/frequency plane and controls the frequency
        bandwidth (thus the frequency resolution) and the number of good
        tapers. See :func:`mne.time_frequency.tfr_array_multitaper`
        documentation.
    n_cycles : float | array_like of float
        Number of cycles in the wavelet, either a fixed number or one per
        frequency. The number of cycles ``n_cycles`` and the frequencies of
        interest ``cwt_freqs`` define the temporal window length. For details,
        see :func:`mne.time_frequency.tfr_array_morlet` documentation.
    gc_n_lags : int
        Number of lags to use for the vector autoregressive model when
        computing Granger causality. Higher values increase computational cost,
        but reduce the degree of spectral smoothing in the results. Only used
        if ``method`` contains any of ``['gc', 'gc_tr']``.
    rank : tuple of array | None
        Two arrays with the rank to project the seed and target data to,
        respectively, using singular value decomposition. If `None`, the rank
        of the data is computed and projected to. Only used if ``method``
        contains any of ``['mic', 'mim', 'gc', 'gc_tr']``.
    decim : int
        To reduce memory usage, decimation factor after time-frequency
        decomposition. Returns ``tfr[…, ::decim]``.
    n_jobs : int
        Number of connections to compute in parallel. Memory mapping must be
        activated. Please see the Notes section for details.
    %(verbose)s

    Returns
    -------
    con : instance of Connectivity | list
        Computed connectivity measure(s). An instance of
        :class:`EpochSpectralConnectivity`, :class:`SpectralConnectivity`
        or a list of instances corresponding to connectivity measures if
        several connectivity measures are specified.
        The shape of each connectivity dataset is
        (n_epochs, n_signals, n_signals, n_freqs) when ``indices`` is `None`,
        (n_epochs, n_nodes, n_nodes, n_freqs) when ``indices`` is specified
        and ``n_nodes = len(indices[0])``, or (n_epochs, 1, 1, n_freqs) when a
        multi-variate method is called regardless of "indices".

    See Also
    --------
    mne_connectivity.spectral_connectivity_epochs
    mne_connectivity.SpectralConnectivity
    mne_connectivity.EpochSpectralConnectivity

    Notes
    -----
    Please note that the interpretation of the measures in this function
    depends on the data and underlying assumptions and does not necessarily
    reflect a causal relationship between brain regions.

    The connectivity measures are computed over time within each epoch and
    optionally averaged over epochs. High connectivity values indicate that
    the phase coupling (interpreted as estimated connectivity) differences
    between signals stay consistent over time.

    The spectral densities can be estimated using a multitaper method with
    digital prolate spheroidal sequence (DPSS) windows, or a continuous wavelet
    transform using Morlet wavelets. The spectral estimation mode is specified
    using the ``mode`` parameter.

    When using the multitaper spectral estimation method, the
    cross-spectral density is computed separately for each taper and aggregated
    using a weighted average, where the weights correspond to the concentration
    ratios between the DPSS windows.

    Spectral estimation using multitaper or Morlet wavelets introduces edge
    effects that depend on the length of the wavelet. To remove edge effects,
    the parameter ``padding`` can be used to prune the edges of the signal.
    Please see the documentation of
    :func:`mne.time_frequency.tfr_array_multitaper` and
    :func:`mne.time_frequency.tfr_array_morlet` for details on wavelet length
    (i.e., time window length).

    By default, the connectivity between all signals is computed (only
    connections corresponding to the lower-triangular part of the connectivity
    matrix). If one is only interested in the connectivity between some
    signals, the "indices" parameter can be used. For example, to compute the
    connectivity between the signal with index 0 and signals "2, 3, 4" (a total
    of 3 connections) one can use the following::

        indices = (np.array([0, 0, 0]),    # row indices
                   np.array([2, 3, 4]))    # col indices

        con = spectral_connectivity_time(data, method='coh',
                                         indices=indices, ...)

    In this case ``con.get_data().shape = (3, n_freqs)``. The connectivity
    scores are in the same order as defined indices.

    For multivariate methods, this is handled differently. If "indices" is
    None, connectivity between all signals will attempt to be computed (this is
    not possible if a Granger causality method is called). If "indices" is
    specified, the seeds and targets are treated as a single connection. For
    example, to compute the connectivity between signals 0, 1, 2 and 3, 4, 5,
    one would use the same approach as above, however the signals would all be
    considered for a single connection and the connectivity scores would have
    the shape (1, n_freqs).

    **Supported Connectivity Measures**

    The connectivity method(s) is specified using the ``method`` parameter. The
    following methods are supported (note: ``E[]`` denotes average over
    epochs). Multiple measures can be computed at once by using a list/tuple,
    e.g., ``['coh', 'pli']`` to compute coherence and PLI.

        'coh' : Coherence given by::

                     | E[Sxy] |
            C = ---------------------
                sqrt(E[Sxx] * E[Syy])

        'mic' : Maximised Imaginary part of Coherency (MIC)
        :footcite:`EwaldEtAl2012` given by:

            :math:`MIC=\Large{\frac{\boldsymbol{\alpha}^T \boldsymbol{E \beta}}
            {\parallel\boldsymbol{\alpha}\parallel \parallel\boldsymbol{\beta}
            \parallel}}`

            where: :math:`\boldsymbol{E}` is the imaginary part of the
            transformed cross-spectral density between seeds and targets; and
            :math:`\boldsymbol{\alpha}` and :math:`\boldsymbol{\beta}` are
            eigenvectors for the seeds and targets, such that
            :math:`\boldsymbol{\alpha}^T \boldsymbol{E \beta}` maximises
            connectivity between the seeds and targets.

        'mim' : Multivariate Interaction Measure (MIM)
        :footcite:`EwaldEtAl2012` given by:

            :math:`MIM=tr(\boldsymbol{EE}^T)`

        'plv' : Phase-Locking Value (PLV) :footcite:`LachauxEtAl1999` given
        by::

            PLV = |E[Sxy/|Sxy|]|

        'ciplv' : Corrected imaginary PLV (ciPLV) :footcite:`BrunaEtAl2018`
        given by::

                             |E[Im(Sxy/|Sxy|)]|
            ciPLV = ------------------------------------
                     sqrt(1 - |E[real(Sxy/|Sxy|)]| ** 2)

        'pli' : Phase Lag Index (PLI) :footcite:`StamEtAl2007` given by::

            PLI = |E[sign(Im(Sxy))]|

        'wpli' : Weighted Phase Lag Index (WPLI) :footcite:`VinckEtAl2011`
        given by::

                      |E[Im(Sxy)]|
            WPLI = ------------------
                      E[|Im(Sxy)|]

        'gc' : State-space Granger Causality (GC) :footcite:`BarnettSeth2015`
        given by:

            :math:`GC = ln\Large{(\frac{\lvert\boldsymbol{S}_{tt}\rvert}{\lvert
            \boldsymbol{S}_{tt}-\boldsymbol{H}_{ts}\boldsymbol{\Sigma}_{ss
            \lvert t}\boldsymbol{H}_{ts}^*\rvert}})`,

            where: :math:`s` and :math:`t` represent the seeds and targets,
            respectively; :math:`\boldsymbol{H}` is the spectral transfer
            function; :math:`\boldsymbol{\Sigma}` is the residuals matrix of
            the autoregressive model; and :math:`\boldsymbol{S}` is
            :math:`\boldsymbol{\Sigma}` transformed by :math:`\boldsymbol{H}`.

        'gc_tr' : State-space GC on time-reversed signals
        :footcite:`BarnettSeth2015,WinklerEtAl2016` given by the same equation
        as for 'gc', but where the autocovariance sequence from which the
        autoregressive model is produced is transposed to mimic the reversal of
        the original signal in time.

    Parallel computation can be activated by setting the ``n_jobs`` parameter.
    Under the hood, this utilizes the ``joblib`` library. For effective
    parallelization, you should activate memory mapping in MNE-Python by
    setting ``MNE_MEMMAP_MIN_SIZE`` and ``MNE_CACHE_DIR``. Activating memory
    mapping will make ``joblib`` store arrays greater than the minimum size on
    disc, and forego direct RAM access for more efficient processing.
    For example, in your code, run

        mne.set_config('MNE_MEMMAP_MIN_SIZE', '10M')
        mne.set_config('MNE_CACHE_DIR', '/dev/shm')

    When ``MNE_MEMMAP_MIN_SIZE=None``, the underlying joblib implementation
    results in pickling and unpickling the whole array each time a pair of
    indices is accessed, which is slow, compared to memory mapping the array.

    This function is based on the ``frites.conn.conn_spec`` implementation in
    Frites.

    .. versionadded:: 0.3

    References
    ----------
    .. footbibliography::
    """
    events = None
    event_id = None
    # extract data from Epochs object
    if isinstance(data, BaseEpochs):
        names = data.ch_names
        sfreq = data.info['sfreq']
        events = data.events
        event_id = data.event_id
        n_epochs, n_signals, n_times = data.get_data().shape
        # Extract metadata from the Epochs data structure.
        # Make Annotations persist through by adding them to the metadata.
        metadata = data.metadata
        if metadata is None:
            annots_in_metadata = False
        else:
            annots_in_metadata = all(
                name not in metadata.columns for name in [
                    'annot_onset', 'annot_duration', 'annot_description'])
        if hasattr(data, 'annotations') and not annots_in_metadata:
            data.add_annotations_to_metadata(overwrite=True)
        metadata = data.metadata
        data = data.get_data()
    else:
        data = np.asarray(data)
        n_epochs, n_signals, n_times = data.shape
        names = np.arange(0, n_signals)
        metadata = None
        if sfreq is None:
            raise ValueError('Sampling frequency (sfreq) is required with '
                             'array input.')

    # check that method is a list
    if isinstance(method, str):
        method = [method]

    # defaults for fmin and fmax
    if fmin is None:
        fmin = np.min(freqs)
        logger.info('Fmin was not specified. Using fmin=min(freqs)')
    if fmax is None:
        fmax = np.max(freqs)
        logger.info('Fmax was not specified. Using fmax=max(freqs).')

    fmin = np.array((fmin,), dtype=float).ravel()
    fmax = np.array((fmax,), dtype=float).ravel()
    if len(fmin) != len(fmax):
        raise ValueError('fmin and fmax must have the same length')
    if np.any(fmin > fmax):
        raise ValueError('fmax must be larger than fmin')

    if len(fmin) != 1 and any(
        this_method in _gc_methods for this_method in method
    ):
        raise ValueError('computing Granger causality on multiple frequency '
                         'bands is not yet supported')

    if any(this_method in _multivariate_methods for this_method in method):
        if not all(this_method in _multivariate_methods for
                   this_method in method):
            raise ValueError(
                'bivariate and multivariate connectivity methods cannot be '
                'used in the same function call')
        multivariate_con = True
    else:
        multivariate_con = False

    # convert kernel width in time to samples
    if isinstance(sm_times, (int, float)):
        sm_times = int(np.round(sm_times * sfreq))

    # convert frequency smoothing from hz to samples
    if isinstance(sm_freqs, (int, float)):
        sm_freqs = int(np.round(max(sm_freqs, 1)))

    # temporal decimation
    if isinstance(decim, int):
        sm_times = int(np.round(sm_times / decim))
        sm_times = max(sm_times, 1)

    # Create smoothing kernel
    kernel = _create_kernel(sm_times, sm_freqs, kernel=sm_kernel)

    # get indices of pairs of (group) regions
    if indices is None:
        if multivariate_con:
            if any(this_method in _gc_methods for this_method in method):
                raise ValueError(
                    'indices must be specified when computing Granger '
                    'causality, as all-to-all connectivity is not supported')
            logger.info('using all indices for multivariate connectivity')
            indices_use = (np.arange(n_signals, dtype=int),
                           np.arange(n_signals, dtype=int))
        else:
            logger.info('only using indices for lower-triangular matrix')
            indices_use = np.tril_indices(n_signals, k=-1)
    else:
        if multivariate_con:
            if (
                len(np.unique(indices[0])) != len(indices[0]) or
                len(np.unique(indices[1])) != len(indices[1])
            ):
                raise ValueError(
                    'seed and target indices cannot contain repeated '
                    'channels for multivariate connectivity')
            if any(this_method in _gc_methods for this_method in method):
                if set(indices[0]).intersection(indices[1]):
                    raise ValueError(
                        'seed and target indices must not intersect when '
                        'computing Granger causality')
        indices_use = check_indices(indices)
    source_idx = indices_use[0]
    target_idx = indices_use[1]
    n_pairs = len(source_idx) if not multivariate_con else 1

    # unique signals for which we actually need to compute the CSD of
    signals_use = np.unique(np.r_[indices_use[0], indices_use[1]])

    # check rank input and compute data ranks if necessary
    if multivariate_con:
        rank = _check_rank_input(rank, data, sfreq, indices_use)
    else:
        rank = None
        gc_n_lags = None

    # check freqs
    if isinstance(freqs, (int, float)):
        freqs = [freqs]
    # array conversion
    freqs = np.asarray(freqs)
    # check order for multiple frequencies
    if len(freqs) >= 2:
        delta_f = np.diff(freqs)
        increase = np.all(delta_f > 0)
        assert increase, "Frequencies should be in increasing order"

    # check that freqs corresponds to at least n_cycles cycles
    dur = float(n_times) / sfreq
    cycle_freq = n_cycles / dur
    if np.any(freqs < cycle_freq):
        raise ValueError('At least one value in n_cycles corresponds to a'
                         'wavelet longer than the signal. Use less cycles, '
                         'higher frequencies, or longer epochs.')
    # check for Nyquist
    if np.any(freqs > sfreq / 2):
        raise ValueError(f'Frequencies {freqs[freqs > sfreq / 2]} Hz are '
                         f'larger than Nyquist = {sfreq / 2:.2f} Hz')

    # compute frequency mask based on specified min/max and decimation factor
    freq_mask = _compute_freq_mask(freqs, fmin, fmax, fskip)

    # the frequency points where we compute connectivity
    freqs = freqs[freq_mask]

    # compute central frequencies
    _f = xr.DataArray(np.arange(len(freqs)), dims=('freqs',),
                      coords=(freqs,))
    foi_s = _f.sel(freqs=fmin, method='nearest').data
    foi_e = _f.sel(freqs=fmax, method='nearest').data
    foi_idx = np.c_[foi_s, foi_e]
    f_vec = freqs[foi_idx].mean(1)

    if faverage:
        n_freqs = len(fmin)
        out_freqs = f_vec
    else:
        n_freqs = len(freqs)
        out_freqs = freqs

    conn = dict()
    conn_patterns = dict()
    for m in method:
        conn[m] = np.zeros((n_epochs, n_pairs, n_freqs))
        conn_patterns[m] = np.full((n_epochs, 2, len(source_idx), n_freqs),
                                   np.nan)
    logger.info('Connectivity computation...')

    # parameters to pass to the connectivity function
    call_params = dict(
        method=method, kernel=kernel, foi_idx=foi_idx,
        source_idx=source_idx, target_idx=target_idx, signals_use=signals_use,
        mode=mode, sfreq=sfreq, freqs=freqs, faverage=faverage,
        n_cycles=n_cycles, mt_bandwidth=mt_bandwidth, gc_n_lags=gc_n_lags,
        rank=rank, decim=decim, padding=padding, kw_cwt={}, kw_mt={},
        n_jobs=n_jobs, verbose=verbose, multivariate_con=multivariate_con)

    for epoch_idx in np.arange(n_epochs):
        logger.info(f'   Processing epoch {epoch_idx+1} / {n_epochs} ...')
        scores, patterns = _spectral_connectivity(data[epoch_idx],
                                                  **call_params)
        for m in method:
            conn[m][epoch_idx] = np.stack(scores[m], axis=0)
            if multivariate_con and patterns[m] is not None:
                conn_patterns[m][epoch_idx] = np.stack(patterns[m], axis=0)
    for m in method:
        if np.isnan(conn_patterns[m]).all():
            conn_patterns[m] = None
        else:
            # epochs x 2 x n_channels x n_freqs
            conn_patterns[m] = conn_patterns[m].transpose((1, 0, 2, 3))

    if indices is None and not multivariate_con:
        conn_flat = conn
        conn = dict()
        for m in method:
            this_conn = np.zeros((n_epochs, n_signals, n_signals) +
                                 conn_flat[m].shape[2:],
                                 dtype=conn_flat[m].dtype)
            this_conn[:, source_idx, target_idx] = conn_flat[m]
            this_conn = this_conn.reshape((n_epochs, n_signals ** 2,) +
                                          conn_flat[m].shape[2:])
            conn[m] = this_conn

    if multivariate_con:
        # UNTIL RAGGED ARRAYS SUPPORTED
        indices = tuple(
            [[np.array(indices_use[0])], [np.array(indices_use[1])]])

    # create the connectivity containers
    out = []
    for m in method:
        store_params = {
            'data': conn[m], 'patterns': conn_patterns[m], 'freqs': out_freqs,
            'n_nodes': n_signals, 'names': names, 'indices': indices,
            'method': method, 'spec_method': mode, 'events': events,
            'event_id': event_id, 'metadata': metadata, 'rank': rank,
            'n_lags': gc_n_lags if m in _gc_methods else None}
        if average:
            store_params['data'] = np.mean(store_params['data'], axis=0)
            if conn_patterns[m] is not None:
                store_params['patterns'] = np.mean(store_params['patterns'],
                                                   axis=1)
            out.append(SpectralConnectivity(**store_params))
        else:
            out.append(EpochSpectralConnectivity(**store_params))

    logger.info('[Connectivity computation done]')

    # return the object instead of list of length one
    if len(out) == 1:
        return out[0]
    else:
        return out


def _spectral_connectivity(data, method, kernel, foi_idx,
                           source_idx, target_idx, signals_use,
                           mode, sfreq, freqs, faverage, n_cycles,
                           mt_bandwidth, gc_n_lags, rank, decim, padding,
                           kw_cwt, kw_mt, n_jobs, verbose, multivariate_con):
    """Estimate time-resolved connectivity for one epoch.

    Parameters
    ----------
    data : array_like, shape (n_channels, n_times)
        Time-series data.
    method : list of str
        List of connectivity metrics to compute.
    kernel : array_like, shape (n_sm_fres, n_sm_times)
        Smoothing kernel.
    foi_idx : array_like, shape (n_foi, 2)
        Upper and lower bound indices of frequency bands.
    source_idx : array_like, shape (n_pairs,)
        Defines the signal pairs of interest together with ``target_idx``.
    target_idx : array_like, shape (n_pairs,)
        Defines the signal pairs of interest together with ``source_idx``.
    signals_use : list of int
        The unique signals on which connectivity is to be computed.
    mode : str
        Time-frequency transformation method.
    sfreq : float
        Sampling frequency.
    freqs : array_like
        Array of frequencies of interest for time-frequency decomposition.
        Only the frequencies within the range specified by ``fmin`` and
        ``fmax`` are used.
    faverage : bool
        Average over frequency bands.
    n_cycles : float | array_like of float
        Number of cycles in the wavelet, either a fixed number or one per
        frequency.
    mt_bandwidth : float | None
        Multitaper time-bandwidth.
    gc_n_lags : int
        Number of lags to use for the vector autoregressive model when
        computing Granger causality.
    rank : tuple of array
        Ranks to project the seed and target data to.
    decim : int
        Decimation factor after time-frequency
        decomposition.
    padding : float
        Amount of time to consider as padding at the beginning and end of each
        epoch in seconds.
    multivariate_con : bool
        Whether or not multivariate connectivity is to be computed.

    Returns
    -------
    scores : dict
        Dictionary containing the connectivity estimates corresponding to the
        metrics in ``method``. Each element is an array of shape (n_pairs,
        n_freqs) or (n_pairs, n_fbands) if ``faverage`` is `True`.

    patterns : dict
        Dictionary containing the connectivity patterns (for reconstructing the
        connectivity components in source-space) corresponding to the metrics
        in ``method``, if multivariate methods are called, else an empty
        dictionary. Each element is an array of shape (2, n_channels, n_freqs)
        or (2, n_channels, 1) if ``faverage`` is `True`, where 2 corresponds to
        the seed and target signals (respectively).
    """
    n_pairs = len(source_idx)
    data = np.expand_dims(data, axis=0)
    if mode == 'cwt_morlet':
        out = tfr_array_morlet(
            data, sfreq, freqs, n_cycles=n_cycles, output='complex',
            decim=decim, n_jobs=n_jobs, **kw_cwt)
        out = np.expand_dims(out, axis=2)  # same dims with multitaper
        weights = None
    elif mode == 'multitaper':
        out = tfr_array_multitaper(
            data, sfreq, freqs, n_cycles=n_cycles,
            time_bandwidth=mt_bandwidth, output='complex', decim=decim,
            n_jobs=n_jobs, **kw_mt)
        if isinstance(n_cycles, (int, float)):
            n_cycles = [n_cycles] * len(freqs)
        mt_bandwidth = mt_bandwidth if mt_bandwidth else 4
        n_tapers = int(np.floor(mt_bandwidth - 1))
        weights = np.zeros((n_tapers, len(freqs), out.shape[-1]))
        for i, (f, n_c) in enumerate(zip(freqs, n_cycles)):
            window_length = np.arange(0., n_c / float(f), 1.0 / sfreq).shape[0]
            half_nbw = mt_bandwidth / 2.
            n_tapers = int(np.floor(mt_bandwidth - 1))
            _, eigvals = dpss_windows(window_length, half_nbw, n_tapers,
                                      sym=False)
            weights[:, i, :] = np.sqrt(eigvals[:, np.newaxis])
            # weights have shape (n_tapers, n_freqs, n_times)
    else:
        raise ValueError("Mode must be 'cwt_morlet' or 'multitaper'.")

    out = np.squeeze(out, axis=0)

    if padding:
        if padding < 0:
            raise ValueError(f'Padding cannot be negative, got {padding}.')
        if padding >= data.shape[-1] / sfreq / 2:
            raise ValueError(f'Padding cannot be larger than half of data '
                             f'length, got {padding}.')
        pad_idx = int(np.floor(padding * sfreq / decim))
        out = out[..., pad_idx:-pad_idx]
        weights = weights[..., pad_idx:-pad_idx] if weights is not None \
            else None

    # compute for each connectivity method
    scores = {}
    patterns = {}
    conn = _parallel_con(out, method, kernel, foi_idx, source_idx, target_idx,
                         signals_use, gc_n_lags, rank, n_jobs, verbose,
                         n_pairs, faverage, weights, multivariate_con)
    for i, m in enumerate(method):
        if multivariate_con:
            scores[m] = conn[0][i]
            patterns[m] = conn[1][i][:, 0] if conn[1][i] is not None else None
        else:
            scores[m] = [out[i] for out in conn]
            patterns[m] = None

    return scores, patterns


###############################################################################
###############################################################################
#                               TIME-RESOLVED CORE FUNCTIONS
###############################################################################
###############################################################################

def _parallel_con(w, method, kernel, foi_idx, source_idx, target_idx,
                  signals_use, gc_n_lags, rank, n_jobs, verbose, total,
                  faverage, weights, multivariate_con):
    """Compute spectral connectivity in parallel.

    Parameters
    ----------
    w : array_like, shape (n_chans, n_tapers, n_freqs, n_times)
        Time-frequency data (complex signal).
    method : list of str
        List of connectivity metrics to compute.
    kernel : array_like, shape (n_sm_fres, n_sm_times)
        Smoothing kernel.
    foi_idx : array_like, shape (n_foi, 2)
        Upper and lower bound indices of frequency bands.
    source_idx : array_like, shape (n_pairs,)
        Defines the signal pairs of interest together with ``target_idx``.
    target_idx : array_like, shape (n_pairs,)
        Defines the signal pairs of interest together with ``source_idx``.
    signals_use : list of int
        The unique signals on which connectivity is to be computed.
    gc_n_lags : int
        Number of lags to use for the vector autoregressive model when
        computing Granger causality.
    rank : tuple of array
        Ranks to project the seed and target data to.
    n_jobs : int
        Number of parallel jobs.
    total : int
        Number of pairs of signals.
    faverage : bool
        Average over frequency bands.
    weights : array_like, shape (n_tapers, n_freqs, n_times)
        Multitaper weights.
    multivariate_con : bool
        Whether or not multivariate connectivity is being computed.

    Returns
    -------
    out : tuple of list of array
        Connectivity estimates for each signal pair, method, and frequency or
        frequency band. If bivariate methods are called, the output is a tuple
        of a list of arrays containing the connectivity scores. If multivariate
        methods are called, the output is a tuple of lists containing arrays
        for the connectivity scores and patterns, respectively.
    """
    if 'coh' in method:
        # psd
        if weights is not None:
            psd = weights * w
            psd = psd * np.conj(psd)
            psd = psd.real.sum(axis=1)
            psd = psd * 2 / (weights * weights.conj()).real.sum(axis=0)
        else:
            psd = w.real ** 2 + w.imag ** 2
            psd = np.squeeze(psd, axis=1)

        # smooth
        psd = _smooth_spectra(psd, kernel)
    else:
        psd = None

    if not multivariate_con:
        # only show progress if verbosity level is DEBUG
        if verbose != 'DEBUG' and verbose != 'debug' and verbose != 10:
            total = None

        # define the function to compute in parallel
        parallel, my_pairwise_con, n_jobs = parallel_func(
            _pairwise_con, n_jobs=n_jobs, verbose=verbose, total=total)

        return tuple(parallel(
            my_pairwise_con(w, psd, s, t, method, kernel, foi_idx, faverage,
                            weights) for s, t in zip(source_idx, target_idx)))

    return _multivariate_con(w, source_idx, target_idx, signals_use, method,
                             kernel, foi_idx, faverage, weights, gc_n_lags,
                             rank, n_jobs)


def _pairwise_con(w, psd, x, y, method, kernel, foi_idx,
                  faverage, weights):
    """Compute spectral connectivity metrics between two signals.

    Parameters
    ----------
    w : array_like, shape (n_chans, n_tapers, n_freqs, n_times)
        Time-frequency data.
    psd : array_like, shape (n_chans, n_freqs, n_times)
        Power spectrum between signals ``x`` and ``y``.
    x : int
        Channel index.
    y : int
        Channel index.
    method : str
        Connectivity method.
    kernel : array_like, shape (n_sm_fres, n_sm_times)
        Smoothing kernel.
    foi_idx : array_like, shape (n_foi, 2)
        Upper and lower bound indices of frequency bands.
    faverage : bool
        Average over frequency bands.
    weights : array_like, shape (n_tapers, n_freqs, n_times) | None
        Multitaper weights.

    Returns
    -------
    out : list
        List of connectivity estimates between signals ``x`` and ``y``
        corresponding to the methods in ``method``. Each element is an array
        with shape (n_freqs,) or (n_fbands) depending on ``faverage``.
    """
    w_x, w_y = w[x], w[y]
    if weights is not None:
        s_xy = np.sum(weights * w_x * np.conj(weights * w_y), axis=0)
        s_xy = s_xy * 2 / (weights * np.conj(weights)).real.sum(axis=0)
    else:
        s_xy = w_x * np.conj(w_y)
        s_xy = np.squeeze(s_xy, axis=0)
    s_xy = _smooth_spectra(s_xy, kernel)
    out = []
    conn_func = {'plv': _plv, 'ciplv': _ciplv, 'pli': _pli, 'wpli': _wpli,
                 'coh': _coh}
    for m in method:
        if m == 'coh':
            s_xx = psd[x]
            s_yy = psd[y]
            out.append(conn_func[m](s_xx, s_yy, s_xy))
        else:
            out.append(conn_func[m](s_xy))

    for i, _ in enumerate(out):
        # mean inside frequency sliding window (if needed)
        if isinstance(foi_idx, np.ndarray) and faverage:
            out[i] = _foi_average(out[i], foi_idx)
        # squeeze time dimension
        out[i] = out[i].squeeze(axis=-1)

    return out


def _multivariate_con(w, source_idx, target_idx, signals_use, method, kernel,
                      foi_idx, faverage, weights, gc_n_lags, rank, n_jobs):
    """Compute spectral connectivity metrics between multiple signals.

    Parameters
    ----------
    w : array_like, shape (n_chans, n_tapers, n_freqs, n_times)
        Time-frequency data.
    x : int
        Channel index.
    y : int
        Channel index.
    method : str
        Connectivity method.
    kernel : array_like, shape (n_sm_fres, n_sm_times)
        Smoothing kernel.
    foi_idx : array_like, shape (n_foi, 2)
        Upper and lower bound indices of frequency bands.
    faverage : bool
        Average over frequency bands.
    weights : array_like, shape (n_tapers, n_freqs, n_times) | None
        Multitaper weights.

    Returns
    -------
    scores : list
        List of connectivity scores between seed and target signals for each
        connectivity method. Each element is an array with shape (n_freqs,) or
        (n_fbands) depending on ``faverage``.

    patterns : list
        List of connectivity patterns between seed and target signals for each
        connectivity method. Each element is an array of length 2 corresponding
        to the seed and target patterns, respectively, each with shape
        (n_channels, n_freqs,) or (n_channels, n_fbands) depending on
        ``faverage``.
    """
    csd = []
    for x in signals_use:
        for y in signals_use:
            w_x, w_y = w[x], w[y]
            if weights is not None:
                s_xy = np.sum(weights * w_x * np.conj(weights * w_y), axis=0)
                s_xy = s_xy * 2 / (weights * np.conj(weights)).real.sum(axis=0)
            else:
                s_xy = w_x * np.conj(w_y)
                s_xy = np.squeeze(s_xy, axis=0)
            csd.append(_smooth_spectra(s_xy, kernel).mean(axis=-1))
    csd = np.array(csd)

    # initialise connectivity estimators and add CSD information
    conn_class = {'mic': _MICEst, 'mim': _MIMEst, 'gc': _GCEst,
                  'gc_tr': _GCTREst}
    conn = []
    for m in method:
        # N_CONS = 1 UNTIL RAGGED ARRAYS SUPPORTED
        call_params = {'n_signals': len(signals_use), 'n_cons': 1,
                       'n_freqs': csd.shape[1], 'n_times': 0,
                       'n_jobs': n_jobs}
        if m in _gc_methods:
            call_params['n_lags'] = gc_n_lags
        con_est = conn_class[m](**call_params)
        for con_i, con_csd in enumerate(csd):
            con_est.accumulate(con_i, con_csd)
        conn.append(con_est)

    # compute connectivity
    scores = []
    patterns = []
    for con_est in conn:
        con_est.compute_con(np.array([source_idx, target_idx]), rank)
        scores.append(con_est.con_scores[..., np.newaxis])
        patterns.append(con_est.patterns)
        if patterns[-1] is not None:
            patterns[-1] = patterns[-1][..., np.newaxis]

    for i, _ in enumerate(scores):
        # mean inside frequency sliding window (if needed)
        if isinstance(foi_idx, np.ndarray) and faverage:
            scores[i] = _foi_average(scores[i], foi_idx)
            if patterns[i] is not None:
                patterns[i] = _foi_average(patterns[i], foi_idx)
        # squeeze time dimension
        scores[i] = scores[i].squeeze(axis=-1)
        if patterns[i] is not None:
            patterns[i] = patterns[i].squeeze(axis=-1)

    return scores, patterns


def _plv(s_xy):
    """Compute phase-locking value given the cross power spectral density.

    Parameters
    ----------
    s_xy : array-like, shape (n_freqs, n_times)
        The cross PSD between channel 'x' and channel 'y' across
        frequency and time points.

    Returns
    -------
    plv : array-like, shape (n_freqs, n_times)
        The estimated PLV.
    """
    s_xy = s_xy / np.abs(s_xy)
    plv = np.abs(s_xy.mean(axis=-1, keepdims=True))
    return plv


def _ciplv(s_xy):
    """Compute corrected imaginary phase-locking value.

    Parameters
    ----------
    s_xy : array-like, shape (n_freqs, n_times)
        The cross PSD between channel 'x' and channel 'y' across
        frequency and time points.

    Returns
    -------
    ciplv : array-like, shape (n_freqs, n_times)
        The estimated ciPLV.
    """
    s_xy = s_xy / np.abs(s_xy)
    rplv = np.abs(np.mean(np.real(s_xy), axis=-1, keepdims=True))
    iplv = np.abs(np.mean(np.imag(s_xy), axis=-1, keepdims=True))
    ciplv = iplv / (np.sqrt(1 - rplv ** 2))
    return ciplv


def _pli(s_xy):
    """Compute phase-lag index given the cross power spectral density.

    Parameters
    ----------
    s_xy : array-like, shape (n_freqs, n_times)
        The cross PSD between channel 'x' and channel 'y' across
        frequency and time points.

    Returns
    -------
    pli : array-like, shape (n_freqs, n_times)
        The estimated PLI.
    """
    pli = np.abs(np.mean(np.sign(np.imag(s_xy)),
                         axis=-1, keepdims=True))
    return pli


def _wpli(s_xy):
    """Compute weighted phase-lag index given the cross power spectral density.

    Parameters
    ----------
    s_xy : array-like, shape (n_freqs, n_times)
        The cross PSD between channel 'x' and channel 'y' across
        frequency and time points.

    Returns
    -------
    wpli : array-like, shape (n_freqs, n_times)
        The estimated wPLI.
    """
    con_num = np.abs(s_xy.imag.mean(axis=-1, keepdims=True))
    con_den = np.mean(np.abs(s_xy.imag), axis=-1, keepdims=True)
    wpli = con_num / con_den
    return wpli


def _coh(s_xx, s_yy, s_xy):
    """Compute coherence given the cross spectral density and PSD.

    Parameters
    ----------
    s_xx : array-like, shape (n_freqs, n_times)
        The PSD of channel 'x'.
    s_yy : array-like, shape (n_freqs, n_times)
        The PSD of channel 'y'.
    s_xy : array-like, shape (n_freqs, n_times)
        The cross PSD between channel 'x' and channel 'y' across
        frequency and time points.

    Returns
    -------
    coh : array-like, shape (n_freqs, n_times)
        The estimated COH.
    """
    con_num = np.abs(s_xy.mean(axis=-1, keepdims=True))
    con_den = np.sqrt(s_xx.mean(axis=-1, keepdims=True) *
                      s_yy.mean(axis=-1, keepdims=True))
    coh = con_num / con_den
    return coh


def _compute_csd(x, y, weights):
    """Compute cross spectral density between signals x and y."""
    if weights is not None:
        s_xy = np.sum(weights * x * np.conj(weights * y), axis=-3)
        s_xy = s_xy * 2 / (weights * np.conj(weights)).real.sum(axis=-3)
    else:
        s_xy = x * np.conj(y)
        s_xy = np.squeeze(s_xy, axis=-3)
    return s_xy


def _foi_average(conn, foi_idx):
    """Average inside frequency bands.

    The frequency dimension should be located at -2.

    Parameters
    ----------
    conn : array_like, shape (..., n_freqs, n_times)
        Connectivity estimate array.
    foi_idx : array_like, shape (n_foi, 2)
        Upper and lower frequency bounds of each frequency band.

    Returns
    -------
    conn_f : np.ndarray, shape (..., n_fbands, n_times)
        Connectivity estimate array, averaged within frequency bands.
    """
    # get the number of foi
    n_foi = foi_idx.shape[0]

    # get input shape and replace n_freqs with the number of foi
    sh = list(conn.shape)
    sh[-2] = n_foi

    # compute average
    conn_f = np.zeros(sh, dtype=conn.dtype)
    for n_f, (f_s, f_e) in enumerate(foi_idx):
        f_e += 1 if f_s == f_e else f_e
        conn_f[..., n_f, :] = conn[..., f_s:f_e, :].mean(-2)
    return conn_f
