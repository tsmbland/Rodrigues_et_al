import numpy as np
from scipy.optimize import differential_evolution
from joblib import Parallel, delayed
import multiprocessing
import os
from .funcs import straighten, rolling_ave_2d, interp_1d_array, interp_2d_array, rotate_roi, save_img, error_func, gaus
from .roi import interp_roi, offset_coordinates, spline_roi


class ImageQuant:
    """
    Quantification works by taking cross sections across the membrane, and fitting the resulting profile as the sum of
    a cytoplasmic signal component and a membrane signal component

    Input data:
    img                image
    roi                coordinates defining cortex

    Fitting parameters:
    sigma              gaussian/error function width
    freedom            amount of freedom allowed in ROI (0=min, 1=max, max offset is +- 0.5 * freedom * thickness)
    periodic           True if coordinates form a closed loop
    thickness          thickness of cross section over which to perform quantification
    itp                amount to interpolate image prior to segmentation (this many points per pixel in original image)
    rol_ave            width of rolling average
    rotate             if True, will automatically rotate ROI so that the first/last points are at the end of the long
                       axis
    zerocap            if True, prevents negative membrane and cytoplasm values
    nfits              performs this many fits at regular intervals around ROI
    iterations         if >1, adjusts ROI and re-fits
    interp             interpolation type (linear or cubic)
    bg_subtract        if True, will estimate and subtract background signal prior to quantification

    Computation:
    parallel           TRUE = perform fitting in parallel
    cores              number of cores to use if parallel is True (if none will use all available)

    Saving:
    save_path          destination to save results, will create if it doesn't already exist

    """

    def __init__(self, img, sigma=None, roi=None, freedom=0.5,
                 periodic=True, thickness=50, itp=10, rol_ave=10, parallel=False, cores=None, rotate=False,
                 zerocap=True, nfits=None, iterations=2, interp='cubic', save_path=None, bg_subtract=False):

        # Image / stackm
        self.img = img

        # ROI
        self.roi_init = roi
        self.roi = roi
        self.periodic = periodic

        # Background subtraction
        self.bg_subtract = bg_subtract

        # Fitting parameters
        self.iterations = iterations
        self.thickness = thickness
        self.itp = itp
        self.thickness_itp = int(itp * self.thickness)
        self.freedom = freedom
        self.rol_ave = rol_ave
        self.rotate = rotate
        self.zerocap = zerocap
        self.sigma = sigma
        self.nfits = nfits
        self.interp = interp

        # Saving
        self.save_path = save_path

        # Background curves
        self.cytbg = (1 + error_func(np.arange(thickness * 2), thickness, self.sigma)) / 2
        self.cytbg_itp = (1 + error_func(np.arange(2 * self.thickness_itp), self.thickness_itp,
                                         self.sigma * self.itp)) / 2
        self.membg = gaus(np.arange(thickness * 2), thickness, self.sigma)
        self.membg_itp = gaus(np.arange(2 * self.thickness_itp), self.thickness_itp, self.sigma * self.itp)

        # Computation
        self.parallel = parallel
        if cores is not None:
            self.cores = cores
        else:
            self.cores = multiprocessing.cpu_count()

        # Results containers
        self.offsets = None
        self.cyts = None
        self.mems = None
        self.offsets_full = None
        self.cyts_full = None
        self.mems_full = None

        # Simulated images
        self.straight = None
        self.straight_filtered = None
        self.straight_fit = None
        self.straight_mem = None
        self.straight_cyt = None
        self.straight_resids = None
        self.straight_resids_pos = None
        self.straight_resids_neg = None

        if self.roi is not None:
            self.reset_res()

    """
    Run

    """

    def run(self):

        # Fitting
        for i in range(self.iterations):
            if i > 0:
                self.adjust_roi()
                self.reset_res()
            self.fit()

        # Simulate images
        self.sim_images()

        # Save
        if self.save_path is not None:
            self.save()

    def fit(self):

        # Specify number of fits
        if self.nfits is None:
            self.nfits = len(self.roi[:, 0])

        # Straighten image
        self.straight = straighten(self.img, self.roi, self.thickness)

        # Background subtract
        if self.bg_subtract:
            bg_intensity = np.mean(self.straight[:5, :])
            self.straight -= bg_intensity
            self.img -= bg_intensity

        # Smoothen
        if self.rol_ave != 0:
            self.straight_filtered = rolling_ave_2d(self.straight, self.rol_ave, self.periodic)
        else:
            self.straight_filtered = self.straight

        # Interpolate
        straight = interp_2d_array(self.straight_filtered, self.thickness_itp, method=self.interp)
        straight = interp_2d_array(straight, self.nfits, ax=0, method=self.interp)

        # Fit
        if self.parallel:
            results = np.array(Parallel(n_jobs=self.cores)(
                delayed(self._fit_profile)(straight[:, x]) for x in range(len(straight[0, :]))))
            self.offsets = results[:, 0]
            self.cyts = results[:, 1]
            self.mems = results[:, 2]
        else:
            for x in range(len(straight[0, :])):
                self.offsets[x], self.cyts[x], self.mems[x] = self._fit_profile(straight[:, x])

        # Interpolate
        self.offsets_full = interp_1d_array(self.offsets, len(self.roi[:, 0]), method='linear')
        self.cyts_full = interp_1d_array(self.cyts, len(self.roi[:, 0]), method='linear')
        self.mems_full = interp_1d_array(self.mems, len(self.roi[:, 0]), method='linear')

    def _fit_profile(self, profile):
        if self.zerocap:
            bounds = (
                ((self.thickness_itp / 2) * (1 - self.freedom), (self.thickness_itp / 2) * (1 + self.freedom)),
                (0, max(2 * max(profile), 0)), (0, max(2 * max(profile), 0)))
        else:
            bounds = (
                ((self.thickness_itp / 2) * (1 - self.freedom), (self.thickness_itp / 2) * (1 + self.freedom)),
                (-0.2 * max(profile), 2 * max(profile)), (-0.2 * max(profile), 2 * max(profile)))
        res = differential_evolution(self._mse, bounds=bounds, args=(profile,), tol=0.2)
        o = (res.x[0] - self.thickness_itp / 2) / self.itp
        return o, res.x[1], res.x[2]

    def _mse(self, l_c_m, profile):
        l, c, m = l_c_m
        y = (c * self.cytbg_itp[int(l):int(l) + self.thickness_itp]) + (
                m * self.membg_itp[int(l):int(l) + self.thickness_itp])
        return np.mean((profile - y) ** 2)

    """
    Misc

    """

    def sim_images(self):
        """
        Creates simulated images based on fit results

        """
        for x in range(len(self.roi[:, 0])):
            c = self.cyts_full[x]
            m = self.mems_full[x]
            l = int(self.offsets_full[x] * self.itp + (self.thickness_itp / 2))
            self.straight_cyt[:, x] = interp_1d_array(c * self.cytbg_itp[l:l + self.thickness_itp], self.thickness,
                                                      method=self.interp)
            self.straight_mem[:, x] = interp_1d_array(m * self.membg_itp[l:l + self.thickness_itp], self.thickness,
                                                      method=self.interp)
            self.straight_fit[:, x] = interp_1d_array(
                (c * self.cytbg_itp[l:l + self.thickness_itp]) + (m * self.membg_itp[l:l + self.thickness_itp]),
                self.thickness, method=self.interp)
            self.straight_resids[:, x] = self.straight[:, x] - self.straight_fit[:, x]
            self.straight_resids_pos[:, x] = np.clip(self.straight_resids[:, x], a_min=0, a_max=None)
            self.straight_resids_neg[:, x] = abs(np.clip(self.straight_resids[:, x], a_min=None, a_max=0))

    def adjust_roi(self):
        """
        Can do after a preliminary fit to refine coordinates
        Must refit after doing this

        """

        # Offset coordinates
        self.roi = offset_coordinates(self.roi, self.offsets_full)

        # Fit spline
        self.roi = spline_roi(roi=self.roi, periodic=self.periodic, s=100)

        # Interpolate to one px distance between points
        self.roi = interp_roi(self.roi, self.periodic)

        # Rotate
        if self.periodic:
            if self.rotate:
                self.roi = rotate_roi(self.roi)

    def reset(self):
        """
        Resets entire class to its initial state

        """

        self.roi = self.roi_init
        self.reset_res()

    def reset_res(self):
        """
        Clears results

        """

        if self.nfits is None:
            self.nfits = len(self.roi[:, 0])

        # Results
        self.offsets = np.zeros(self.nfits)
        self.cyts = np.zeros(self.nfits)
        self.mems = np.zeros(self.nfits)

        # Interpolated results
        self.offsets_full = np.zeros(len(self.roi[:, 0]))
        self.cyts_full = np.zeros(len(self.roi[:, 0]))
        self.mems_full = np.zeros(len(self.roi[:, 0]))

        # Simulated images
        self.straight = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_filtered = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_fit = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_mem = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_cyt = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_resids = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_resids_pos = np.zeros([self.thickness, len(self.roi[:, 0])])
        self.straight_resids_neg = np.zeros([self.thickness, len(self.roi[:, 0])])

    def save(self):
        """
        Save all results to save_path

        """

        if not os.path.isdir(self.save_path):
            os.mkdir(self.save_path)

        np.savetxt(self.save_path + '/offsets.txt', self.offsets, fmt='%.4f', delimiter='\t')
        np.savetxt(self.save_path + '/cyts.txt', self.cyts, fmt='%.4f', delimiter='\t')
        np.savetxt(self.save_path + '/mems.txt', self.mems, fmt='%.4f', delimiter='\t')
        np.savetxt(self.save_path + '/roi.txt', self.roi, fmt='%.4f', delimiter='\t')
        save_img(self.img, self.save_path + '/img.tif')
        save_img(self.straight, self.save_path + '/straight.tif')
        # save_img(self.straight_filtered, self.save_path + '/straight_filtered.tif')
        save_img(self.straight_fit, self.save_path + '/straight_fit.tif')
        # save_img(self.straight_mem, self.save_path + '/straight_mem.tif')
        # save_img(self.straight_cyt, self.save_path + '/straight_cyt.tif')
        # save_img(self.straight_resids, self.save_path + '/straight_resids.tif')
        # save_img(self.straight_resids_pos, self.save_path + '/straight_resids_pos.tif')
        # save_img(self.straight_resids_neg, self.save_path + '/straight_resids_neg.tif')
