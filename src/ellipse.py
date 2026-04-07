"""Ellipse fitting and Mahalanobis distance computation."""
from __future__ import annotations

import math
import numpy as np


class EllipseFitter:
    """
    Fits a Gaussian ellipse to a set of geographic points and computes
    the Mahalanobis distance of any point from the ellipse center.

    The Mahalanobis distance accounts for the shape and orientation of the
    ellipse — a point at distance 0.3 is deep in the center, at 2.0 it's
    on the edge.
    """

    def __init__(self, points: np.ndarray):
        """
        Args:
            points: Nx2 array of (lat, lng) coordinates.
        """
        self.center = points.mean(axis=0)
        self.cov = np.cov(points.T)
        self.n_points = len(points)

        try:
            self.cov_inv = np.linalg.inv(self.cov)
            self.valid = True
        except np.linalg.LinAlgError:
            self.cov_inv = None
            self.valid = False

        # Ellipse properties
        if self.valid:
            eigenvalues = np.linalg.eigvalsh(self.cov)
            self.semi_major = 2 * math.sqrt(max(eigenvalues))
            self.semi_minor = 2 * math.sqrt(min(eigenvalues))
            self.eccentricity = math.sqrt(
                1 - eigenvalues.min() / eigenvalues.max()
            ) if eigenvalues.max() > 0 else 0.0
        else:
            self.semi_major = 0.0
            self.semi_minor = 0.0
            self.eccentricity = 0.0

    def mahalanobis(self, lat: float, lng: float) -> float | None:
        """Compute Mahalanobis distance from the ellipse center."""
        if not self.valid:
            return None
        diff = np.array([lat, lng]) - self.center
        return float(np.sqrt(diff @ self.cov_inv @ diff))

    def euclidean(self, lat: float, lng: float) -> float:
        """Euclidean distance from center in degrees."""
        diff = np.array([lat, lng]) - self.center
        return float(np.sqrt((diff ** 2).sum()))

    def angle(self, lat: float, lng: float) -> float:
        """Angle from center in radians."""
        diff = np.array([lat, lng]) - self.center
        return float(np.arctan2(diff[1], diff[0]))

    @property
    def lat_spread(self) -> float:
        return float(np.sqrt(self.cov[0, 0])) if self.valid else 0.0

    @property
    def lng_spread(self) -> float:
        return float(np.sqrt(self.cov[1, 1])) if self.valid else 0.0