# -*- coding: utf-8 -*-
"""
:class:`~pycroscopy.analysis.be_loop_fitter.BESHOfitter` that fits band
excitation cantilever vibration response to a Simple Harmonic Oscillator model

Created on Thu Nov 20 11:48:53 2019

@author: Suhas Somnath, Chris R. Smith
"""
from __future__ import division, print_function, absolute_import, \
    unicode_literals
from enum import Enum
from warnings import warn
import numpy as np
from functools import partial
from pyUSID.io.hdf_utils import copy_region_refs, write_simple_attrs, \
    create_results_group, write_reduced_anc_dsets, create_empty_dataset, \
    write_main_dataset
from pyUSID.io.usi_data import USIDataset

# From this project:
from utils import sho_error, reshape_to_one_step, reshape_to_n_steps, \
    is_reshapable, complex_gaussian, wavelet_peaks
from fitter import Fitter


'''
Custom dtype for the datasets created during fitting.
'''
field_names = ['Amplitude [V]', 'Frequency [Hz]', 'Quality Factor',
               'Phase [rad]', 'R2 Criterion']
sho32 = np.dtype({'names': field_names,
                  'formats': [np.float32 for name in field_names]})


class SHOGuessFunc(Enum):
    complex_gaussian = 0
    wavelet_peaks = 1


class SHOFitFunc(Enum):
    least_squares = 0


class BESHOfitter(Fitter):
    
    def __init__(self, h5_main, **kwargs):
        """
        Creates an instance of the BESHOFitter class

        Parameters
        ----------
        h5_main : pyUSID.io.USIDataset
            Main dataset containing band excitation measurement
        kwargs : dict, optional
            Keyword arguments such as "verbose" and "cores" that will be
            passed onto :class:`~pyUSID.processing.process.Process`
        """
        super(BESHOfitter, self).__init__(h5_main, variables=['Frequency'],
                                          **kwargs)

        self.process_name = "SHO_Fit"
        self.parms_dict = None
        
        self._fit_dim_name = 'Frequency'      

        # Extract some basic parameters that are necessary for either the guess
        # or fit
        freq_dim_ind = self.h5_main.spec_dim_labels.index('Frequency')
        self.step_start_inds = np.where(self.h5_main.h5_spec_inds[freq_dim_ind] == 0)[0]
        self.num_udvs_steps = len(self.step_start_inds)

        # find the frequency vector and hold in memory
        self.freq_vec = None
        self._get_frequency_vector()

        # This is almost always True but think of this more as a sanity check.
        self.is_reshapable = is_reshapable(self.h5_main, self.step_start_inds)

        # accounting for memory copies
        self._max_raw_pos_per_read = self._max_pos_per_read
        # set limits in the set up functions

        self.results_pix_byte_size = sho32.itemsize * self.num_udvs_steps
            
    def _get_frequency_vector(self):
        """
        Reads the frequency vector from the Spectroscopic_Values dataset.  
        This assumes that the data is reshape-able.
        
        """
        h5_spec_vals = self.h5_main.h5_spec_vals
        freq_dim = np.argwhere('Frequency' == np.array(self.h5_main.spec_dim_labels)).squeeze()

        if len(self.step_start_inds) == 1:  # BE-Line
            end_ind = h5_spec_vals.shape[1]
        else:  # BEPS
            end_ind = self.step_start_inds[1]

        self.freq_vec = h5_spec_vals[freq_dim, self.step_start_inds[0]:end_ind]
          
    def _create_guess_datasets(self):
        """
        Creates the h5 group, guess dataset, corresponding spectroscopic datasets and also
        links the guess dataset to the spectroscopic datasets.
        """
        self.h5_results_grp = create_results_group(self.h5_main, self.process_name)
        write_simple_attrs(self.h5_results_grp, self.parms_dict)

        h5_sho_inds, h5_sho_vals = write_reduced_anc_dsets(self.h5_results_grp, self.h5_main.h5_spec_inds,
                                                            self.h5_main.h5_spec_vals, self._fit_dim_name)

        self._h5_guess = write_main_dataset(self.h5_results_grp, (self.h5_main.shape[0], self.num_udvs_steps), 'Guess', 'SHO',
                                           'compound', None, None, h5_pos_inds=self.h5_main.h5_pos_inds,
                                           h5_pos_vals=self.h5_main.h5_pos_vals, h5_spec_inds=h5_sho_inds,
                                           h5_spec_vals=h5_sho_vals, chunks=(1, self.num_udvs_steps), dtype=sho32,
                                           main_dset_attrs=self.parms_dict, verbose=self.verbose)
        
        copy_region_refs(self.h5_main, self._h5_guess)
        
        self._h5_guess.file.flush()
        
        if self.verbose and self.mpi_rank == 0:
            print('Finished creating Guess dataset')
        
    def _create_fit_datasets(self):
        """
        Creates the HDF5 fit dataset. pycroscopy requires that the h5 group, guess dataset,
        corresponding spectroscopic and position datasets be created and populated at this point.
        This function will create the HDF5 dataset for the fit and link it to same ancillary datasets as the guess.
        The fit dataset will NOT be populated here but will instead be populated using the __setData function
        """

        if self._h5_guess is None or self.h5_results_grp is None:
            warn('Need to guess before fitting!')
            return

        """
        Once the guess is complete, the last_pixel attribute will be set to complete for the group.
        Once the fit is initiated, during the creation of the status dataset, this last_pixel
        attribute will be used and it wil make the fit look like it was already complete. Which is not the case.
        This is a problem of doing two processes within the same group. 
        Until all legacy is removed, we will simply reset the last_pixel attribute.
        """
        self.h5_results_grp.attrs['last_pixel'] = 0

        write_simple_attrs(self.h5_results_grp, self.parms_dict)

        # Create the fit dataset as an empty dataset of the same size and dtype
        # as the guess.
        # Also automatically links in the ancillary datasets.
        self._h5_fit = USIDataset(create_empty_dataset(self._h5_guess,
                                                       dtype=sho32,
                                                       dset_name='Fit'))

        self._h5_fit.file.flush()
        
        if self.verbose and self.mpi_rank == 0:
            print('Finished creating Fit dataset')
        
    def _read_data_chunk(self):
        """
        Returns the next chunk of data for the guess or the fit
        """

        # The Fitter class should take care of all the basic reading
        super(BESHOfitter, self)._read_data_chunk()

        # At this point the self.data object is the raw data that needs to be
        # reshaped to a single UDVS step:
        if self.data is not None:
            if self.verbose and self.mpi_rank == 0:
                print('Got raw data of shape {} from super'
                      '.'.format(self.data.shape))
            self.data = reshape_to_one_step(self.data, self.num_udvs_steps)
            if self.verbose and self.mpi_rank == 0:
                print('Reshaped raw data to shape {}'.format(self.data.shape))
                
    def _read_guess_chunk(self):
        """
        Returns a chunk of guess dataset corresponding to the main dataset.

        Parameters
        -----
        None

        Returns
        --------

        """
        # The Fitter class should take care of all the basic reading
        super(BESHOfitter, self)._read_guess_chunk()
        
        self._guess = reshape_to_one_step(self._guess, self.num_udvs_steps)
        # bear in mind that this self._guess is a compound dataset. Convert to float32
        # don't keep the R^2.
        self._guess = np.hstack([self._guess[name] for name in self._guess.dtype.names if name != 'R2 Criterion'])
                    
    def _write_results_chunk(self):
        """
        Writes the provided chunk of data into the guess or fit datasets. 
        This method is responsible for any and all book-keeping.
        """
        prefix = 'guess' if self._is_guess else 'fit'
        self._results = self._reformat_results(self._results,
                                               self.parms_dict[prefix + '-algorithm'])
        
        if self._is_guess:
            self._guess = np.hstack(tuple(self._results))
            # prepare to reshape:
            self._guess = np.transpose(np.atleast_2d(self._guess))
            if self.verbose and self.mpi_rank == 0:
                print('Prepared guess of shape {} before reshaping'.format(self._guess.shape))
            self._guess = reshape_to_n_steps(self._guess, self.num_udvs_steps)
            if self.verbose and self.mpi_rank == 0:
                print('Reshaped guess to shape {}'.format(self._guess.shape))
        else:
            self._fit = self._results
            self._fit = np.transpose(np.atleast_2d(self._fit))
            self._fit = reshape_to_n_steps(self._fit, self.num_udvs_steps)

        # ask super to take care of the rest, which is a standardized operation
        super(BESHOfitter, self)._write_results_chunk()
                   
    def set_up_guess(self, guess_func=SHOGuessFunc.complex_gaussian, 
                     *func_args, h5_partial_guess=None, **func_kwargs):
        """
        Need this because during the set up, we won't know which strategy is being used.
        Should Guess be its own Process class in that case? If so, it would end up having 
        its own group etc.
        
        TODO: Move generic code to Fitter once LoopFit is completed as a second example
        """
        self.parms_dict = {'guess-method': "pycroscopy BESHO"}
        
        if not isinstance(guess_func, SHOGuessFunc):
            raise TypeError('Please supply SHOGuessFunc.complex_gaussian or SHOGuessFunc.wavelet_peaks for the guess_func')
        
        partial_func = None
        
        if guess_func == SHOGuessFunc.complex_gaussian:
            
            num_points=func_kwargs.pop('num_points', 5)
            
            self.parms_dict.update({'guess-algorithm': 'complex_gaussian',
                                    'guess-complex_gaussian-num_points': num_points})
            
            partial_func = partial(complex_gaussian, w_vec=self.freq_vec, 
                                   num_points=num_points)
            
        elif guess_func == SHOGuessFunc.wavelet_peaks:
            
            peak_width_bounds = func_kwargs.pop('peak_width_bounds', [10, 200])
            peak_width_step = func_kwargs.pop('peak_width_step', 20)

            if len(func_args) > 0:
                # Assume that the first argument is what we are looking for
                peak_width_bounds = func_args[0]
            
            self.parms_dict.update({'guess_algorithm': 'wavelet_peaks',
                                    'guess-wavelet_peaks-peak_width_bounds': peak_width_bounds,
                                    'guess-wavelet_peaks-peak_width_step': peak_width_step})

            partial_func = partial(wavelet_peaks, peak_width_bounds=peak_width_bounds, 
                                   peak_width_step=peak_width_step, **func_kwargs)

        self._map_function = partial_func

        self._max_pos_per_read = self._max_raw_pos_per_read // 1.5

        # ask super to take care of the rest, which is a standardized operation
        super(BESHOfitter, self).set_up_guess(h5_partial_guess=h5_partial_guess)
        
    def set_up_fit(self, fit_func=SHOFitFunc.least_squares, 
                   *func_args, h5_partial_fit=None, h5_guess=None, **func_kwargs):
        """
        Need this because during the set up, we won't know which strategy is being used.
        Should Guess be its own Process class in that case? If so, it would end up having 
        its own group etc.
        """
        self.parms_dict = {'fit-method': "pycroscopy BESHO"}
        
        if not isinstance(fit_func, SHOFitFunc):
            raise TypeError('Please supply SHOFitFunc.least_squares for the fit_func')
                
        if fit_func == SHOFitFunc.least_squares:
                                    
            self.parms_dict.update({'fit-algorithm': 'least_squares'})

        self._max_pos_per_read = self._max_raw_pos_per_read // 1.75

        # ask super to take care of the rest, which is a standardized operation
        super(BESHOfitter, self).set_up_fit(h5_partial_fit=h5_partial_fit,
                                            h5_guess=h5_guess)
           
    def _unit_compute_fit(self):
        """
        Punts unit computation on a chunk of data to Process

        """
        super(BESHOfitter, self)._unit_compute_fit(sho_error,
                                                   obj_func_args=[self.freq_vec],
                                                   solver_options={'jac': 'cs'})

    def _reformat_results(self, results, strategy='wavelet_peaks'):
        """
        Model specific calculation and or reformatting of the raw guess or fit results

        Parameters
        ----------
        results : array-like
            Results to be formatted for writing
        strategy : str
            The strategy used in the fit.  Determines how the results will be reformatted.
            Default 'wavelet_peaks'

        Returns
        -------
        sho_vec : numpy.ndarray
            The reformatted array of parameters.
            
        """
        if self.verbose and self.mpi_rank == 0:
            print('Strategy to use for reformatting results: "{}"'.format(strategy))
        # Create an empty array to store the guess parameters
        sho_vec = np.zeros(shape=(len(results)), dtype=sho32)
        if self.verbose and self.mpi_rank == 0:
            print('Raw results and compound SHO vector of shape {}'.format(len(results)))

        # Extracting and reshaping the remaining parameters for SHO
        if strategy in ['wavelet_peaks', 'relative_maximum', 'absolute_maximum']:
            if self.verbose and self.mpi_rank == 0:
                  print('Reformatting results from a peak-position-finding algorithm')
            # wavelet_peaks sometimes finds 0, 1, 2, or more peaks. Need to handle that:
            # peak_inds = np.array([pixel[0] for pixel in results])
            peak_inds = np.zeros(shape=(len(results)), dtype=np.uint32)
            for pix_ind, pixel in enumerate(results):
                if len(pixel) == 1:  # majority of cases - one peak found
                    peak_inds[pix_ind] = pixel[0]
                elif len(pixel) == 0:  # no peak found
                    peak_inds[pix_ind] = int(0.5*self.data.shape[1])  # set to center of band
                else:  # more than one peak found
                    dist = np.abs(np.array(pixel) - int(0.5*self.data.shape[1]))
                    peak_inds[pix_ind] = pixel[np.argmin(dist)]  # set to peak closest to center of band
            if self.verbose and self.mpi_rank == 0:
                print('Peak positions of shape {}'.format(peak_inds.shape))
            # First get the value (from the raw data) at these positions:
            comp_vals = np.array(
                [self.data[pixel_ind, peak_inds[pixel_ind]] for pixel_ind in np.arange(peak_inds.size)])
            if self.verbose and self.mpi_rank == 0:
                print('Complex values at peak positions of shape {}'.format(comp_vals.shape))
            sho_vec['Amplitude [V]'] = np.abs(comp_vals)  # Amplitude
            sho_vec['Phase [rad]'] = np.angle(comp_vals)  # Phase in radians
            sho_vec['Frequency [Hz]'] = self.freq_vec[peak_inds]  # Frequency
            sho_vec['Quality Factor'] = np.ones_like(comp_vals) * 10  # Quality factor
            # Add something here for the R^2
            sho_vec['R2 Criterion'] = np.array([self.r_square(self.data, self._sho_func, self.freq_vec, sho_parms)
                                                for sho_parms in sho_vec])
        elif strategy in ['complex_gaussian']:
            if self.verbose and self.mpi_rank == 0:
                print('Reformatting results from the SHO Guess algorithm')
            for iresult, result in enumerate(results):
                sho_vec['Amplitude [V]'][iresult] = result[0]
                sho_vec['Frequency [Hz]'][iresult] = result[1]
                sho_vec['Quality Factor'][iresult] = result[2]
                sho_vec['Phase [rad]'][iresult] = result[3]
                sho_vec['R2 Criterion'][iresult] = result[4]
        elif strategy in ['least_squares']:
            if self.verbose and self.mpi_rank == 0:
                print('Reformatting results from a list of least_squares result objects')
            for iresult, result in enumerate(results):
                sho_vec['Amplitude [V]'][iresult] = result.x[0]
                sho_vec['Frequency [Hz]'][iresult] = result.x[1]
                sho_vec['Quality Factor'][iresult] = result.x[2]
                sho_vec['Phase [rad]'][iresult] = result.x[3]
                sho_vec['R2 Criterion'][iresult] = 1-result.fun
        else:
            if self.verbose and self.mpi_rank == 0:
                  print('_reformat_results() will not reformat results since the provided algorithm: {} does not match anything that this function can handle.'.format(strategy))

        return sho_vec
