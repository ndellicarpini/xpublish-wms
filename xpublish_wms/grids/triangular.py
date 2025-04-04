from typing import Optional, Sequence, Union

import dask.array as dask_array
import matplotlib.tri as tri
import numpy as np
import xarray as xr

import time

from xpublish_wms.grids.grid import Grid, RenderMethod
from xpublish_wms.utils import (
    barycentric_weights,
    lat_lng_find_tri,
    lat_lng_find_tri_ind,
    strip_float,
    to_mercator,
    to_lnglat
)


class TriangularGrid(Grid):
    filtered_element_ind = []
    def __init__(self, ds: xr.Dataset):
        self.ds = ds

    @staticmethod
    def recognize(ds: xr.Dataset) -> bool:
        return ds.attrs.get("grid_type", "").lower().startswith("triangular")

    @property
    def name(self) -> str:
        return "triangular"

    @property
    def render_method(self) -> RenderMethod:
        return RenderMethod.Triangle

    @property
    def crs(self) -> str:
        return "EPSG:4326"

    def has_elevation(self, da: xr.DataArray) -> bool:
        return "vertical" in da.cf

    def elevation_units(self, da: xr.DataArray) -> Optional[str]:
        if "vertical" in da.cf:
            return da.cf["vertical"].attrs.get("units", "sigma")

        return None

    def elevation_positive_direction(self, da: xr.DataArray) -> Optional[str]:
        if "vertical" in da.cf:
            return da.cf["vertical"].attrs.get("positive", "up")

        return None

    def elevations(self, da: xr.DataArray) -> Optional[xr.DataArray]:
        if "vertical" in da.cf:
            return da.cf["vertical"][:, 0]

        return None

    def sel_lat_lng(
        self,
        subset: xr.Dataset,
        lng,
        lat,
        parameters,
    ) -> tuple[xr.Dataset, list, list]:
        """Select the given dataset by the given lon/lat and optional elevation"""
        subset = self.mask(subset)

        lng_values = self.ds.cf["longitude"].values
        lat_values = self.ds.cf["latitude"].values

        if lng < 0 and np.max(lng_values) > 180:
            lng += 360
        elif lng > 180 and np.min(lng_values) < 0:
            lng -= 360

        # find if the selected lng/lat is within a triangle
        valid_tri = lat_lng_find_tri_ind(
            lng,
            lat,
            lng_values,
            lat_values,
            self.tessellate(subset),
        )

        ret_subset = subset.isel(node=0)

        # if no -> set all values to nan
        if valid_tri is None:
            for parameter in parameters:
                ret_subset.__setitem__(
                    parameter,
                    (
                        ret_subset[parameter].dims,
                        np.full(ret_subset[parameter].shape, np.nan),
                        ret_subset[parameter].attrs,
                    ),
                )
        else:
            valid_element = self.ds.element[valid_tri].values[0].astype(int)
            p1 = [lng_values[valid_element[0]], lat_values[valid_element[0]]]
            p2 = [lng_values[valid_element[1]], lat_values[valid_element[1]]]
            p3 = [lng_values[valid_element[2]], lat_values[valid_element[2]]]
            w1, w2, w3 = barycentric_weights([lng, lat], p1, p2, p3)

            for parameter in parameters:
                values = subset[parameter].values

                v1 = values[..., valid_element[0]]
                v2 = values[..., valid_element[1]]
                v3 = values[..., valid_element[2]]

                new_value = (v1 * w1) + (v2 * w2) + (v3 * w3)
                ret_subset.__setitem__(
                    parameter,
                    (
                        ret_subset[parameter].dims,
                        np.full(ret_subset[parameter].shape, new_value),
                        ret_subset[parameter].attrs,
                    ),
                )

        x_axis = [strip_float(ret_subset.cf["longitude"])]
        y_axis = [strip_float(ret_subset.cf["latitude"])]
        return ret_subset, x_axis, y_axis

    def select_by_elevation(
        self,
        da: xr.DataArray,
        elevations: Optional[Sequence[float]],
    ) -> xr.DataArray:
        """Select the given data array by elevation"""
        if not self.has_elevation(da):
            return da

        if (
            elevations is None
            or len(elevations) == 0
            or all(v is None for v in elevations)
        ):
            elevations = [0.0]

        da_elevations = self.elevations(da)

        elevation_index = [
            int(np.absolute(da_elevations - elevation).argmin().values)
            for elevation in elevations
        ]
        if len(elevation_index) == 1:
            elevation_index = elevation_index[0]

        if "vertical" in da.cf:
            da = da.cf.isel(vertical=elevation_index)

        return da

    def additional_coords(self, da):
        filter_dims = ["nele", "node", "mesh", "nvertex", "nbou", "nope", "nvel_dim"]
        return [dim for dim in super().additional_coords(da) if dim not in filter_dims]

    def project(
        self,
        da: xr.DataArray,
        crs: str,
    ) -> tuple[xr.DataArray, Optional[xr.DataArray], Optional[xr.DataArray]]:
        da = self.mask(da)
        
        if crs == "EPSG:4326":
            adjust_lng = 0
            if np.min(da.cf["longitude"]) < -180:
                adjust_lng = 360
            elif np.max(da.cf["longitude"]) > 180:
                adjust_lng = -360

            x = da.cf["longitude"] + adjust_lng
            y = da.cf["latitude"]
        elif crs == "EPSG:3857":
            x, y = to_mercator.transform(da.cf["longitude"], da.cf["latitude"])

        x_chunks = (
            da.cf["longitude"].chunks if da.cf["longitude"].chunks else x.shape
        )
        y_chunks = da.cf["latitude"].chunks if da.cf["latitude"].chunks else y.shape

        da = da.assign_coords(
            {
                "x": (
                    da.cf["longitude"].dims,
                    dask_array.from_array(x, chunks=x_chunks),
                    da.cf["longitude"].attrs,
                ),
                "y": (
                    da.cf["latitude"].dims,
                    dask_array.from_array(y, chunks=y_chunks),
                    da.cf["latitude"].attrs,
                ),
            },
        )

        da = da.unify_chunks()

        return da, None, None
    
    def filter_by_bbox(self, 
        da: Union[xr.DataArray, xr.Dataset], 
        bbox: tuple[float, float, float, float],
        crs: str
    ) -> Union[xr.DataArray, xr.Dataset]:
        # if crs == "EPSG:3857":
        #     bbox = to_lnglat.transform([bbox[0], bbox[2]], [bbox[1], bbox[3]])
        #     bbox = np.array(bbox).flatten().tolist()

        # adjust_lng = 0
        # if np.min(da.cf["longitude"]) < -180:
        #     adjust_lng = 360
        # elif np.max(da.cf["longitude"]) > 180:
        #     adjust_lng = -360

        # x = da.cf["longitude"] + adjust_lng
        # y = da.cf["latitude"]

        # x = np.where((x >= bbox[0]) & (x <= bbox[2]))[0]
        # y = np.where((y >= bbox[1]) & (y <= bbox[3]))[0]

        # e = self.ds.element.values
        # e_ind = np.intersect1d(x, y)
        # node_ind = e[np.any(np.isin(e.flat, e_ind).reshape(e.shape), axis=1)]

        # da = da.isel(node=np.unique(node_ind.flat))
        # da = da.unify_chunks()
        return da

    def tessellate(self, da: Union[xr.DataArray, xr.Dataset]) -> np.ndarray:
        nv = self.ds.element
        if len(nv.shape) > 2:
            for i in range(len(nv.shape) - 2):
                nv = nv[0]

        return tri.Triangulation(
            da.cf["longitude"],
            da.cf["latitude"],
            nv - 1,
        ).triangles