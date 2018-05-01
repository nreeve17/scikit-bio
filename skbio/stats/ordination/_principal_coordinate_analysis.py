# ----------------------------------------------------------------------------
# Copyright (c) 2013--, scikit-bio development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
# ----------------------------------------------------------------------------

import numpy as np
import pandas as pd
from numpy import dot, hstack
from numpy.linalg import qr, svd
from numpy.random import standard_normal
from scipy.linalg import eigh
from warnings import warn

from skbio.stats.distance import DistanceMatrix
from skbio.util._decorator import experimental
from ._ordination_results import OrdinationResults
from ._utils import center_distance_matrix_optimized

@experimental(as_of="0.4.0")
def pcoa(distance_matrix, method="eigh", to_dimension=None,
         normalize_eigenvectors=False):
    r"""Perform Principal Coordinate Analysis.

    Principal Coordinate Analysis (PCoA) is a method similar in principle
    to Principle Components Analysis (PCA) with the difference that PCoA
    operates on distance matrices, typically with non-euclidian and thus
    ecologically meaningful distances like UniFrac in microbiome research.

    In ecology, the euclidean distance preserved by Principal
    Component Analysis (PCA) is often not a good choice because it
    deals poorly with double zeros (Species have unimodal
    distributions along environmental gradients, so if a species is
    absent from two sites at the same site, it can't be known if an
    environmental variable is too high in one of them and too low in
    the other, or too low in both, etc. On the other hand, if an
    species is present in two sites, that means that the sites are
    similar.).

    Parameters
    ----------
    distance_matrix : DistanceMatrix
        A distance matrix.
    method : str
        Eigendecomposition method to use in performing PCoA.
        By default, uses SciPy's "eigh", which computes exact
        eigenvectors and eigenvalues for all dimensions. The alternate
        method, "fsvd", uses faster heuristic eigendecomposition but loses
        accuracy. The magnitude of accuracy lost is dependent on dataset.
    to_dimension : number
        Dimensions to reduce the distance matrix to. This number determines
        how many eigenvectors and eigenvalues will be returned.
        By default, equal to the number of dimensions of the distance matrix,
        as default eigendecompsition using SciPy's "eigh" method computes
        all eigenvectors and eigenvalues. If using fast heuristic
        eigendecomposition through "fsvd", a desired dimension should be
        specified.
    normalize_eigenvectors : bool
        False by default. If True, normalizes eigenvectors into
        unit vectors.

    Returns
    -------
    OrdinationResults
        Object that stores the PCoA results, including eigenvalues, the
        proportion explained by each of them, and transformed sample
        coordinates.

    See Also
    --------
    OrdinationResults

    Notes
    -----
    It is sometimes known as metric multidimensional scaling or
    classical scaling.

    .. note::

       If the distance is not euclidean (for example if it is a
       semimetric and the triangle inequality doesn't hold),
       negative eigenvalues can appear. There are different ways
       to deal with that problem (see Legendre & Legendre 1998, \S
       9.2.3), but none are currently implemented here.

       However, a warning is raised whenever negative eigenvalues
       appear, allowing the user to decide if they can be safely
       ignored.
    """
    distance_matrix_obj = DistanceMatrix(distance_matrix)

    # Center distance matrix, a requirement for PCoA here
    distance_matrix = center_distance_matrix_optimized(
        distance_matrix_obj.data)

    # If no dimension specified, by default will compute all eigenvectors
    # and eigenvalues
    if to_dimension is None:
        # distance_matrix is guaranteed to be square
        to_dimension = distance_matrix.shape[0]

    # Perform eigendecomposition
    if method == "eigh":
        eigvals, eigvecs = eigh(distance_matrix)
        long_method_name = "Principal Coordinate Analysis"
    elif method == "fsvd":
        eigvals, eigvecs = _fsvd(distance_matrix, to_dimension)
        long_method_name = "Approximate Principal Coordinate Analysis " \
                           "using FSVD"
    else:
        raise ValueError(
            "PCoA eigendecomposition method {} not supported.".format(method))

    # cogent makes eigenvalues positive by taking the
    # abs value, but that doesn't seem to be an approach accepted
    # by L&L to deal with negative eigenvalues. We raise a warning
    # in that case. First, we make values close to 0 equal to 0.
    negative_close_to_zero = np.isclose(eigvals, 0)
    eigvals[negative_close_to_zero] = 0
    if np.any(eigvals < 0):
        warn(
            "The result contains negative eigenvalues."
            " Please compare their magnitude with the magnitude of some"
            " of the largest positive eigenvalues. If the negative ones"
            " are smaller, it's probably safe to ignore them, but if they"
            " are large in magnitude, the results won't be useful. See the"
            " Notes section for more details. The smallest eigenvalue is"
            " {0} and the largest is {1}.".format(eigvals.min(),
                                                  eigvals.max()),
            RuntimeWarning
        )

    # eigvals might not be ordered, so we first sort them, then analogously
    # sort the eigenvectors by the ordering of the eigenvalues too
    idxs_descending = eigvals.argsort()[::-1]
    eigvals = eigvals[idxs_descending]
    eigvecs = eigvecs[:, idxs_descending]

    # If we return only the coordinates that make sense (i.e., that have a
    # corresponding positive eigenvalue), then Jackknifed Beta Diversity
    # won't work as it expects all the OrdinationResults to have the same
    # number of coordinates. In order to solve this issue, we return the
    # coordinates that have a negative eigenvalue as 0
    num_positive = (eigvals >= 0).sum()
    eigvecs[:, num_positive:] = np.zeros(eigvecs[:, num_positive:].shape)
    eigvals[num_positive:] = np.zeros(eigvals[num_positive:].shape)

    # Normalize eigenvectors to unit length
    if normalize_eigenvectors:
        eigvecs = np.apply_along_axis(lambda vec: vec / np.linalg.norm(vec),
                                      axis=1, arr=eigvecs)

    # Scale eigenvalues to have length = sqrt(eigenvalue). This
    # works because np.linalg.eigh returns normalized
    # eigenvectors. Each row contains the coordinates of the
    # objects in the space of principal coordinates. Note that at
    # least one eigenvalue is zero because only n-1 axes are
    # needed to represent n points in an euclidean space.
    coordinates = eigvecs * np.sqrt(eigvals)

    # Calculate the array of proportion of variance explained
    proportion_explained = eigvals / eigvals.sum()

    axis_labels = list(["PC%d" % i for i in range(1, to_dimension + 1)])
    return OrdinationResults(
        short_method_name="PCoA",
        long_method_name=long_method_name,
        eigvals=pd.Series(eigvals, index=axis_labels),
        samples=pd.DataFrame(coordinates, index=distance_matrix_obj.ids,
                             columns=axis_labels),
        proportion_explained=pd.Series(proportion_explained,
                                       index=axis_labels))


def _fsvd(centered_distance_matrix, dimension=3,
          use_power_method=False, num_levels=1):
    """
           Performs singular value decomposition, or more specifically in
           this case eigendecomposition, using fast heuristic algorithm
           nicknamed "FSVD" (FastSVD), adapted and optimized from the algorithm
           described by Halko et al (2011).

           Parameters
           ----------
           centered_distance_matrix: np.array
               Numpy matrix representing the distance matrix for which the
               eigenvectors and eigenvalues shall be computed
           dimension: int
               Number of dimensions to keep. Must be lower than or equal to the
               rank of the given distance_matrix.
           num_levels: int
               Number of levels of the Krylov method to use (see paper).
               For most applications, num_levels=1 or num_levels=2 is
               sufficient.
           use_power_method: bool
               Changes the power of the spectral norm, thus minimizing
               the error). See paper p11/eq8.1 DOI = {10.1137/100804139}

           Returns
           -------
           np.array
               Array of eigenvectors, each with num_dimensions_out length.
           np.array
               Array of eigenvalues, a total number of num_dimensions_out.

           Notes
           -----
           The algorithm is based on 'An Algorithm for the Principal
           Component analysis of Large Data Sets'
           by N. Halko, P.G. Martinsson, Y. Shkolnisky, and M. Tygert.
           Original Paper: https://arxiv.org/abs/1007.5510

           Ported from reference MATLAB implementation: https://goo.gl/JkcxQ2
           """

    m, n = centered_distance_matrix.shape

    # Note: this transpose is removed for performance, since we
    # only expect square matrices.
    # Take (conjugate) transpose if necessary, because it makes H smaller,
    # leading to faster computations
    # if m < n:
    #     distance_matrix = distance_matrix.transpose()
    #     m, n = distance_matrix.shape
    if m != n:
        raise ValueError('FSVD.run(...) expects square distance matrix')

    k = dimension + 2

    # Form a real nxl matrix G whose entries are independent,
    # identically distributed
    # Gaussian random variables of
    # zero mean and unit variance
    G = standard_normal(size=(n, k))

    if use_power_method:
        # use only the given exponent
        H = dot(centered_distance_matrix, G)

        for x in range(2, num_levels + 2):
            # enhance decay of singular values
            # note: distance_matrix is no longer transposed, saves work
            # since we're expecting symmetric, square matrices anyway
            # (Daniel McDonald's changes)
            H = dot(centered_distance_matrix, dot(centered_distance_matrix, H))

    else:
        # compute the m x l matrices H^{(0)}, ..., H^{(i)}
        # Note that this is done implicitly in each iteration below.
        H = dot(centered_distance_matrix, G)
        # Again, removed transpose: dot(distance_matrix.transpose(), H)
        # to enhance performance
        H = hstack(
            (H,
             dot(centered_distance_matrix, dot(centered_distance_matrix, H))))
        for x in range(3, num_levels + 2):
            # Removed this transpose: dot(distance_matrix.transpose(), H)
            tmp = dot(centered_distance_matrix,
                      dot(centered_distance_matrix, H))

            # Removed this transpose: dot(distance_matrix.transpose(), tmp)
            H = hstack(
                (H, dot(centered_distance_matrix,
                        dot(centered_distance_matrix, tmp))))

    # Using the pivoted QR-decomposition, form a real m * ((i+1)l) matrix Q
    # whose columns are orthonormal, s.t. there exists a real
    # ((i+1)l) * ((i+1)l) matrix R for which H = QR
    Q, R = qr(H)

    # Compute the n * ((i+1)l) product matrix T = A^T Q
    # Removed transpose of distance_matrix for performance
    T = dot(centered_distance_matrix, Q)  # step 3

    # Form an SVD of T
    Vt, St, W = svd(T, full_matrices=False)
    W = W.transpose()

    # Compute the m * ((i+1)l) product matrix
    Ut = dot(Q, W)

    if m < n:
        # V_fsvd = Ut[:, :num_dimensions_out] # unused
        U_fsvd = Vt[:, :dimension]
    else:
        # V_fsvd = Vt[:, :num_dimensions_out] # unused
        U_fsvd = Ut[:, :dimension]

    S = St[:dimension] ** 2

    # drop imaginary component, if we got one
    # Note:
    # - In cogent, after computing eigenvalues/vectors, the imaginary part
    #   is dropped, if any. We know for a fact that the eigenvalues are
    #   real, so that's not necessary, but eigenvectors can in principle
    #   be complex (see for example
    #   http://math.stackexchange.com/a/47807/109129 for details)
    eigenvalues = S.real
    eigenvectors = U_fsvd.real

    return eigenvalues, eigenvectors
