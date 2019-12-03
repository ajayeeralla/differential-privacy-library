# MIT License
#
# Copyright (C) IBM Corporation 2019
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
#
# New BSD License
#
# Copyright (c) 2007–2019 The scikit-learn developers.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
# following conditions are met:
#
#   a. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
#      disclaimer.
#   b. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the
#      following disclaimer in the documentation and/or other materials provided with the distribution.
#   c. Neither the name of the Scikit-learn Developers  nor the names of its contributors may be used to endorse or
#      promote products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
"""
Standard Scaler with differential privacy
"""
import numpy as np
import sklearn.preprocessing as sk_pp
from sklearn.preprocessing.data import _handle_zeros_in_scale
from sklearn.utils import check_array
from sklearn.utils.validation import FLOAT_DTYPES

from diffprivlib.tools import nanvar, nanmean

range_ = range


def _incremental_mean_and_var(X, epsilon, range, last_mean, last_variance, last_sample_count):
    """Calculate mean update and a Youngs and Cramer variance update.

    last_mean and last_variance are statistics computed at the last step by the function. Both must be initialized to
    0.0. In case no scaling is required last_variance can be None. The mean is always required and returned because
    necessary for the calculation of the variance. last_n_samples_seen is the number of samples encountered until now.

    From the paper "Algorithms for computing the sample variance: analysis and recommendations", by Chan, Golub,
    and LeVeque.

    Parameters
    ----------
    X : array-like, shape (n_samples, n_features)
        Data to use for variance update

    last_mean : array-like, shape: (n_features,)

    last_variance : array-like or None, shape: (n_features,)

    last_sample_count : array-like, shape (n_features,)

    Returns
    -------
    updated_mean : array, shape (n_features,)

    updated_variance : array, shape (n_features,)
        If None, only mean is computed

    updated_sample_count : array, shape (n_features,)

    Notes
    -----
    NaNs are ignored during the algorithm.

    References
    ----------
    T. Chan, G. Golub, R. LeVeque. Algorithms for computing the sample
        variance: recommendations, The American Statistician, Vol. 37, No. 3,
        pp. 242-247

    Also, see the sparse implementation of this in
    `utils.sparsefuncs.incr_mean_variance_axis` and
    `utils.sparsefuncs_fast.incr_mean_variance_axis0`
    """
    # old = stats until now
    # new = the current increment
    # updated = the aggregated stats
    last_sum = last_mean * last_sample_count
    new_mean = nanmean(X, epsilon=epsilon, axis=0, range=range)
    new_sample_count = np.sum(~np.isnan(X), axis=0)
    new_sum = new_mean * new_sample_count
    updated_sample_count = last_sample_count + new_sample_count

    updated_mean = (last_sum + new_sum) / updated_sample_count

    if last_variance is None:
        updated_variance = None
    else:
        new_unnormalized_variance = nanvar(X, epsilon=epsilon, axis=0, range=range) * new_sample_count
        last_unnormalized_variance = last_variance * last_sample_count

        with np.errstate(divide='ignore', invalid='ignore'):
            last_over_new_count = last_sample_count / new_sample_count
            updated_unnormalized_variance = (
                last_unnormalized_variance + new_unnormalized_variance +
                last_over_new_count / updated_sample_count *
                (last_sum / last_over_new_count - new_sum) ** 2)

        zeros = last_sample_count == 0
        updated_unnormalized_variance[zeros] = new_unnormalized_variance[zeros]
        updated_variance = updated_unnormalized_variance / updated_sample_count

    return updated_mean, updated_variance, updated_sample_count


class StandardScaler(sk_pp.StandardScaler):
    def __init__(self, epsilon=1, range=None, copy=True, with_mean=True, with_std=True):
        super().__init__(copy=copy, with_mean=with_mean, with_std=with_std)
        self.epsilon = epsilon
        self.range = range

    def partial_fit(self, X, y=None):
        """Online computation of mean and std with differential privacy on X for later scaling. All of X is processed as
        a single batch. This is intended for cases when `fit` is not feasible due to very large number of `n_samples` or
        because X is read from a continuous stream.

        The algorithm for incremental mean and std is given in Equation 1.5a,b in Chan, Tony F., Gene H. Golub, and
        Randall J. LeVeque. "Algorithms for computing the sample variance: Analysis and recommendations." The American
        Statistician 37.3 (1983): 242-247:

        Parameters
        ----------
        X : {array-like}, shape [n_samples, n_features]
            The data used to compute the mean and standard deviation used for later scaling along the features axis.

        y
            Ignored
        """

        epsilon_0 = self.epsilon if self.with_std is None else self.epsilon / 2

        X = check_array(X, accept_sparse=False, copy=self.copy, warn_on_dtype=True, estimator=self, dtype=FLOAT_DTYPES,
                        force_all_finite='allow-nan')

        # Even in the case of `with_mean=False`, we update the mean anyway. This is needed for the incremental
        # computation of the var See incr_mean_variance_axis and _incremental_mean_variance_axis

        # if n_samples_seen_ is an integer (i.e. no missing values), we need to transform it to a NumPy array of
        # shape (n_features,) required by incr_mean_variance_axis and _incremental_variance_axis
        if hasattr(self, 'n_samples_seen_') and isinstance(self.n_samples_seen_, (int, np.integer)):
            self.n_samples_seen_ = np.repeat(self.n_samples_seen_, X.shape[1]).astype(np.int64)

        if not hasattr(self, 'n_samples_seen_'):
            self.n_samples_seen_ = np.zeros(X.shape[1], dtype=np.int64)

        # First pass
        if not hasattr(self, 'scale_'):
            self.mean_ = .0
            if self.with_std:
                self.var_ = .0
            else:
                self.var_ = None

        if not self.with_mean and not self.with_std:
            self.mean_ = None
            self.var_ = None
            self.n_samples_seen_ += X.shape[0] - np.isnan(X).sum(axis=0)
        else:
            self.mean_, self.var_, self.n_samples_seen_ = _incremental_mean_and_var(X, epsilon_0, self.range,
                                                                                    self.mean_, self.var_,
                                                                                    self.n_samples_seen_)

        # for backward-compatibility, reduce n_samples_seen_ to an integer
        # if the number of samples is the same for each feature (i.e. no
        # missing values)
        if np.ptp(self.n_samples_seen_) == 0:
            self.n_samples_seen_ = self.n_samples_seen_[0]

        if self.with_std:
            self.scale_ = _handle_zeros_in_scale(np.sqrt(self.var_))
        else:
            self.scale_ = None

        return self