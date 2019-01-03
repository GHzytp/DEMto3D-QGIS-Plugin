# -*- coding: utf-8 -*-
"""
/***************************************************************************
 AppONCE
                                 A QGIS plugin
 Creación de mapas en 3D
                              -------------------
        begin                : 2015-03-17
        git sha              : $Format:%H$
        copyright            : (C) 2015 by Francisco Javier Venceslá Simón
        email                : jawensi@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
from builtins import range
from qgis.PyQt import QtCore
import collections
import copy

from qgis.PyQt.QtCore import QThread
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsPoint, QgsCoordinateTransform
import math
from osgeo import gdal
import struct


class Model(QThread):
    """Class where is built the mesh point that describe the surface model """
    pto = collections.namedtuple('pto', 'x y z')
    updateProgress = QtCore.pyqtSignal()

    def __init__(self, bar, label, button, parameters):
        QThread.__init__(self)
        self.bar = bar
        self.label = label
        self.button = button
        self.parameters = parameters
        self.matrix_dem = []

        self.quit = False
        self.button.clicked.connect(self.cancel)

    def run(self):
        row_stl = int(math.ceil(self.parameters["height"] / self.parameters["spacing_mm"]) + 1)
        self.bar.setMaximum(row_stl)
        self.bar.setValue(0)
        QApplication.processEvents()

        dem_dataset = gdal.Open(self.parameters["layer"])
        if False:
            self.matrix_dem = self.matrix_dem_builder(dem_dataset, self.parameters["height"], self.parameters["width"],
                                                      self.parameters["scale"], self.parameters["spacing_mm"],
                                                      self.parameters["roi_x_max"], self.parameters["roi_x_min"],
                                                      self.parameters["roi_y_min"], self.parameters["z_base"],
                                                      self.parameters["z_scale"], self.parameters["projected"])
        else:
            self.matrix_dem = self.matrix_dem_builder_interpolation(dem_dataset,
                                                                    self.parameters["height"], self.parameters["width"],
                                                                    self.parameters["scale"],
                                                                    self.parameters["scale_h"],
                                                                    self.parameters["scale_w"],
                                                                    self.parameters["spacing_mm"],
                                                                    self.parameters["roi_x_max"],
                                                                    self.parameters["roi_x_min"],
                                                                    self.parameters["roi_y_min"],
                                                                    self.parameters["z_base"],
                                                                    self.parameters["z_scale"],
                                                                    self.parameters["projected"])
        if self.parameters["z_inv"]:
            self.matrix_dem = self.matrix_dem_inverse_build(self.matrix_dem)
        dem_dataset = None

    def matrix_dem_builder(self, dem_dataset, height, width, scale, spacing_mm,
                           roi_x_max, roi_x_min, roi_y_min, h_base, z_scale, projected):

        # Calculate DEM parameters
        dem_col = dem_dataset.RasterXSize
        dem_row = dem_dataset.RasterYSize
        geotransform = dem_dataset.GetGeoTransform()
        dem_x_min = geotransform[0]
        dem_y_max = geotransform[3]
        dem_y_min = dem_y_max + dem_row * geotransform[5]
        dem_x_max = dem_x_min + dem_col * geotransform[1]

        if not projected:
            spacing_deg = spacing_mm * (roi_x_max - roi_x_min) / width

        row_stl = int(math.ceil(height / spacing_mm) + 1)
        col_stl = int(math.ceil(width / spacing_mm) + 1)
        matrix_dem = [list(range(col_stl)) for i in range(row_stl)]

        var_y = height
        for i in range(row_stl):
            self.updateProgress.emit()
            QApplication.processEvents()
            var_x = 0
            for j in range(col_stl):
                # Model coordinate x(mm), y(mm)
                x_model = round(var_x, 2)
                y_model = round(var_y, 2)

                # Model maps geo_coordinates
                if projected:
                    x = x_model * scale / 1000 + roi_x_min
                    y = y_model * scale / 1000 + roi_y_min
                else:
                    x = x_model * spacing_deg / spacing_mm + roi_x_min
                    y = y_model * spacing_deg / spacing_mm + roi_y_min

                # Model layer geo_coordinates to query z value
                point = QgsPoint(x, y)
                source = self.parameters["crs_map"]
                target = self.parameters["crs_layer"]
                if source != target:
                    transform = QgsCoordinateTransform(source, target)
                    point = transform.transform(point)
                    x = point.x()
                    y = point.y()

                # From x(m) get Column in DEM file
                col_dem = (x - dem_x_min) * dem_col / (dem_x_max - dem_x_min)
                col_dem = int(math.floor(col_dem))
                if col_dem == dem_col:
                    col_dem -= 1
                # From y(m) get Row in DEM file
                row_dem = (dem_y_max - y) * dem_row / (dem_y_max - dem_y_min)
                row_dem = int(math.floor(row_dem))
                if row_dem == dem_row:
                    row_dem -= 1

                # Model coordinate z(mm)
                if col_dem < 0 or row_dem < 0:
                    z_model = 2
                elif self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0] <= h_base:
                    z_model = 2
                elif math.isnan(self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0]):
                    z_model = 2
                else:
                    z_model = round((self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0] - h_base) / scale * 1000 * z_scale, 2) + 2

                matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)

                var_x += spacing_mm
                if var_x > width:
                    var_x = width
            var_y = spacing_mm * (row_stl - (i + 2))
            if self.quit:
                return 0
        return matrix_dem

    def matrix_dem_builder_interpolation(self, dem_dataset, height, width, scale, scale_h, scale_w, spacing_mm,
                                         roi_x_max, roi_x_min, roi_y_min, z_base, z_scale, projected):

        # Calculate DEM parameters
        columns = dem_dataset.RasterXSize
        rows = dem_dataset.RasterYSize
        geotransform = dem_dataset.GetGeoTransform()
        dem_x_min = geotransform[0]  # Limit pixel (not center)
        dem_y_max = geotransform[3]  # Limit pixel (not center)

        # dem_y_min = dem_y_max + rows * geotransform[5]
        # dem_x_max = dem_x_min + columns * geotransform[1]

        spacing_deg = 0
        if not projected:
            spacing_deg = spacing_mm * (roi_x_max - roi_x_min) / width

        row_stl = int(math.ceil(height / spacing_mm) + 1)
        col_stl = int(math.ceil(width / spacing_mm) + 1)
        matrix_dem = [list(range(col_stl)) for i in range(row_stl)]

        var_y = height
        for i in range(row_stl):
            self.updateProgress.emit()
            QApplication.processEvents()
            var_x = 0
            for j in range(col_stl):
                # Model coordinate x(mm), y(mm)
                x_model = round(var_x, 2)
                y_model = round(var_y, 2)

                # Model maps geo_coordinates
                if projected:
                    x = x_model * scale_w / 1000 + roi_x_min
                    y = y_model * scale_h / 1000 + roi_y_min
                else:
                    x = x_model * spacing_deg / spacing_mm + roi_x_min
                    y = y_model * spacing_deg / spacing_mm + roi_y_min

                # Model layer geo_coordinates to query z value
                point = QgsPoint(x, y)
                source = self.parameters["crs_map"]
                target = self.parameters["crs_layer"]
                if source != target:
                    transform = QgsCoordinateTransform(source, target)
                    point = transform.transform(point)
                    x = point.x()
                    y = point.y()

                # From x(m) get Column in DEM file
                col_dem = (x - dem_x_min) / geotransform[1]
                if col_dem >= columns:
                    col_dem -= 1
                # From y(m) get Row in DEM file
                row_dem = (y - dem_y_max) / geotransform[5]
                if row_dem >= rows:
                    row_dem -= 1

                # region nearest neighbours interpolation
                # row_dem = int(math.floor(row_dem))
                # col_dem = int(math.floor(col_dem))
                #
                # # Model coordinate z(mm)
                # if col_dem < 0 or row_dem < 0:
                #     z_model = 2
                # elif self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0] <= h_base:
                #     z_model = 2
                # elif math.isnan(self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0]):
                #     z_model = 2
                # else:
                #     z_model = round((self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0] - h_base) /
                #                     scale * 1000 * z_scale, 2) + 2
                #
                # matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)
                # endregion

                # region Lineal interpolation
                if 0 < col_dem < columns - 1 and 0 < row_dem < rows - 1:
                    min_col = int(math.floor(col_dem))
                    max_col = int(math.ceil(col_dem))
                    min_row = int(math.floor(row_dem))
                    max_row = int(math.ceil(row_dem))

                    # - From geographic coordinates calculate pixel coordinates
                    # - round up and down to see the 4 pixels neighbours integer

                    xP1 = dem_x_min + min_col * geotransform[1]
                    yP1 = dem_y_max + min_row * geotransform[5]
                    zP1 = self.get_z(min_col, min_row, dem_dataset, z_base, scale, z_scale)

                    xP2 = dem_x_min + max_col * geotransform[1]
                    yP2 = dem_y_max + min_row * geotransform[5]
                    zP2 = self.get_z(max_col, min_row, dem_dataset, z_base, scale, z_scale)

                    xP3 = dem_x_min + min_col * geotransform[1]
                    yP3 = dem_y_max + max_row * geotransform[5]
                    zP3 = self.get_z(min_col, max_row, dem_dataset, z_base, scale, z_scale)

                    xP4 = dem_x_min + max_col * geotransform[1]
                    yP4 = dem_y_max + max_row * geotransform[5]
                    zP4 = self.get_z(max_col, max_row, dem_dataset, z_base, scale, z_scale)

                    p = self.pto(x=x, y=y, z=0)
                    p1 = self.pto(x=xP1, y=yP1, z=zP1)
                    p2 = self.pto(x=xP2, y=yP2, z=zP2)
                    p3 = self.pto(x=xP3, y=yP3, z=zP3)
                    p4 = self.pto(x=xP4, y=yP4, z=zP4)

                    z_model = self.interp_line(p, p1, p2, p3, p4)
                    matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)

                else:
                    # Solution for boundaries when col = 0 o col = Nº cols
                    # Manage Boundary limits:
                    if (col_dem == 0 or col_dem >= columns - 1) and (row_dem == 0 or row_dem >= rows - 1):
                        # Corners:
                        col_dem = int(col_dem)
                        row_dem = int(row_dem)
                        z_model = self.get_z(col_dem, row_dem, dem_dataset, z_base, scale, z_scale)
                        matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)

                    elif (col_dem == 0 or col_dem >= columns - 1) and 0 < row_dem < rows - 1:
                        # First and last column
                        min_row = int(math.floor(row_dem))
                        max_row = int(math.ceil(row_dem))
                        col_dem = int(col_dem)

                        if min_row == max_row:
                            z_model = self.get_z(col_dem, max_row, dem_dataset, z_base, scale, z_scale)
                            matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)
                        else:
                            yP1 = dem_y_max + min_row * geotransform[5]
                            zP1 = self.get_z(col_dem, min_row, dem_dataset, z_base, scale, z_scale)

                            yP2 = dem_y_max + max_row * geotransform[5]
                            zP2 = self.get_z(col_dem, max_row, dem_dataset, z_base, scale, z_scale)

                            z_model = zP2 + math.fabs(yP2 - y) * (zP1 - zP2) / math.fabs(yP2 - yP1)
                            matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)

                    elif 0 < col_dem < columns - 1 and (row_dem == 0 or row_dem >= rows - 1):
                        # First and last row
                        min_col = int(math.floor(col_dem))
                        max_col = int(math.ceil(col_dem))
                        row_dem = int(row_dem)

                        if min_col == max_col:
                            z_model = self.get_z(min_col, row_dem, dem_dataset, z_base, scale, z_scale)
                            matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)
                        else:
                            xP1 = dem_x_min + min_col * geotransform[1]
                            zP1 = self.get_z(min_col, row_dem, dem_dataset, z_base, scale, z_scale)

                            xP2 = dem_x_min + max_col * geotransform[1]
                            zP2 = self.get_z(max_col, row_dem, dem_dataset, z_base, scale, z_scale)

                            z_model = zP1 + math.fabs(xP1 - x) * (zP2 - zP1) / math.fabs(xP2 - xP1)
                            matrix_dem[i][j] = self.pto(x=x_model, y=y_model, z=z_model)
                # endregion

                var_x += spacing_mm
                if var_x > width:
                    var_x = width
            var_y = spacing_mm * (row_stl - (i + 2))
            if self.quit:
                return 0

        return matrix_dem

    def get_z(self, col_dem, row_dem, dem_dataset, h_base, scale, z_scale):
        if col_dem < 0 or row_dem < 0:
            return 2
        else:
            z = self.get_dem_z(dem_dataset, col_dem, row_dem, 1, 1)[0]
            if z <= h_base:
                return 2
            elif math.isnan(z):
                return 2
            else:
                return round((z - h_base) / scale * 1000 * z_scale, 2) + 2

    @staticmethod
    def matrix_dem_inverse_build(matrix_dem_build):
        rows = matrix_dem_build.__len__()
        cols = matrix_dem_build[0].__len__()

        matrix_dem = copy.deepcopy(matrix_dem_build)
        z_max = getattr(matrix_dem_build[0][0], "z")
        for i in range(rows):
            for j in range(cols):
                if getattr(matrix_dem_build[i][j], "z") > z_max:
                    z_max = getattr(matrix_dem_build[i][j], "z")
        for i in range(rows):
            for j in range(cols):
                new_z = z_max - getattr(matrix_dem_build[i][j], "z") + 2
                matrix_dem[i][j] = matrix_dem[i][j]._replace(z=new_z)
        return matrix_dem

    @staticmethod
    def get_dem_z(dem_dataset, x_off, y_off, col_size, row_size):
        try:
            band = dem_dataset.GetRasterBand(1)
            data_types = {'Byte': 'B', 'UInt16': 'H', 'Int16': 'h', 'UInt32': 'I', 'Int32': 'i', 'Float32': 'f', 'Float64': 'd'}
            data_type = band.DataType
            data = band.ReadRaster(x_off, y_off, col_size, row_size, col_size, row_size, data_type)
            data = struct.unpack(data_types[gdal.GetDataTypeName(band.DataType)] * col_size * row_size, data)
            return data
        except struct.error:
            return [0]

    @staticmethod
    def interp_line(p, p1, p2, p3, p4):
        try:
            d1 = math.fabs(p2.x - p1.x)
            d2 = math.fabs(p1.y - p3.y)
            dif_z1 = p2.z - p1.z
            dif_z2 = p4.z - p3.z
            if d1 == 0 and d2 == 0 and (p.x - p1.x == 0):
                return p1.x
            if d1 == 0 and (p.x - p1.x == 0):
                return math.fabs(p.y - p3.y) * (p1.z - p3.z) / d2 + p3.z
            elif d2 == 0 and (p1.y - p.y == 0):
                return math.fabs(p.x - p1.x) * dif_z1 / d1 + p1.z
            else:
                zt = math.fabs(p.x - p1.x) * dif_z1 / d1 + p1.z
                zb = math.fabs(p.x - p1.x) * dif_z2 / d1 + p3.z
                return (p1.y - p.y) * (zb - zt) / d2 + zt
        except ZeroDivisionError as err:
            print('Bilineal interpolation error:', err)

    def get_model(self):
        return self.matrix_dem

    def cancel(self):
        self.quit = True
