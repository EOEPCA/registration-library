import json
import logging
import os
import sys
import warnings
import osgeo
from typing import Union, Any
from os import R_OK, access
from os.path import isfile
from registration_library.base.colormapper import ColorMap

from osgeo import gdal, osr
from rasterio import open as rio_open
from rasterio.errors import NotGeoreferencedWarning

gdal.UseExceptions()
gdal.ConfigurePythonLogging(enable_debug=True)

log = logging.getLogger(__name__)


def has_georeference(source: str) -> bool:
    """Validation method to check if source file is georeferenced.

    Args:
        source [str]: file path to source dataset

    Returns:
        A boolean indicating whether the input file has a georeference
    """
    readable = False
    warnings.filterwarnings("error", category=NotGeoreferencedWarning, module="rasterio")
    try:
        source_ds = rio_open(source)
        log.debug(
            f"Checking: {source}\n  crs: {source_ds.crs}\n  "
            f"gcps: {source_ds.crs}\n  transform: {source_ds.crs}"
        )

        if source_ds is not None:
            readable = True
    except Exception as e:
        log.debug(f"File is not georeferenced: {source}.. {e}")
    finally:
        del source_ds
    return readable


def translate(
    source: str,
    target: str,
    format: str,
    options: str,
    max_size: int = None,
    band: int = None,
    subdataset: str = None,
    color_table: str = None,
    color_options: str = None,
    config_options: dict = None,
) -> None:
    """Executes gdal translate (see: https://gdal.org/programs/gdal_translate.html)

    Args:
        source [str]: filepath to source file
        target [str]: filepath to target file
        format [str]: file format
        options [str]: command line options for translate command
        max_size [int]: maximum size of output raster bands (pixels)
        band [int]: reduce input raster to single band by index
        subdataset [str]: reduce input NetCDF File to subdataset
        color_table [str]: path to color table file for output raster
        color_options [str]: path to color_options file for output DEM
        config_options [dict]: dictionary with gdal config key-value pairs to set

    Returns:
        None

    Raises:
        Exception if the gdal process did not run successfully
    """

    if config_options:
        prev_config_options = {key: gdal.GetConfigOption(key) for key in config_options.keys()}
        for key, val in config_options.items():
            gdal.SetConfigOption(key, val)

    gdal.ConfigurePythonLogging(enable_debug=False)
    gdal.UseExceptions()
    translate_output = target

    log.info(
        f"""Translate:
    format: {format}
    options: {options}
    max_size: {max_size}
    band: {band}
    subdataset: {subdataset}
    color_table: {color_table}
    color_options: {color_options}
    config_options: {config_options}
    """
    )

    try:
        log.info(f"Opening: {source}")
        # Open source dataset
        source_ds = gdal.Open(source)

        # Check subdatasets
        if subdataset:
            subdatasets = source_ds.GetSubDatasets()
            subdatasets_string = ""
            sd_name = None
            for sd in subdatasets:
                subdatasets_string += f"\n  {sd[0]}"
                if sd[0] == subdataset or sd[0].endswith(subdataset):
                    sd_name = sd[0]
            log.info(f"Checking subdataset: {subdataset}{subdatasets_string}")
            if sd_name:
                log.info(f"Found subdataset:\n  {sd_name}")
                source_ds = gdal.Open(sd_name)

        # Check band
        if band:
            bands = source_ds.GetBands()
            bands_string = ""
            for b in bands:
                bands_string += f"  {b}\n"
            log.info(f"Checking bands: {band}\n{bands_string}")

        # Define basic properties and check scale/size parameters
        band = source_ds.GetRasterBand(1)
        nodata = band.GetNoDataValue() if band else None

        # Define translate options based on provided values
        if options:
            translate_options = options
        else:
            translate_options = ""

        # Assume EPSG:4326 if not found in source
        epsg = get_EPSG(source_ds)
        if not epsg:
            log.warning("Input does not have an EPSG code, assuming EPSG:4326")
            epsg = 4326
            translate_options += f" -a_srs EPSG:{epsg}"

        scale = get_option(translate_options, "scale")
        log.info(f"Provided scale: {scale}")
        # scale to min/max if -scale parameter used without input or output range
        # see also https://gdal.org/programs/gdal_translate.html#cmdoption-gdal_translate-scale
        if "-scale" in translate_options and not scale:
            minmax = band.ComputeRasterMinMax()
            scale = "{} {} 0 255".format(minmax[0], minmax[1])
            translate_options += f" -scale {scale} -ot Byte"

        # if no outsize is given use max_size if provided by user
        outsize = get_option(translate_options, "outsize")
        if not outsize and max_size:
            if band.XSize >= band.YSize:
                ratio = band.XSize / max_size
            else:
                ratio = band.YSize / max_size
            width = int(band.XSize / ratio)
            height = int(band.YSize / ratio)
            translate_options += f" -outsize {width} {height}"

        # if colortable is provided overwrite output format and create intermediate scaled output
        log.debug(str(color_table))
        if format is None:
            format = get_option(
                translate_options,
                "of",
                exception_message="Either '-of' or 'format' must be provided.",
            )

        if color_table:
            translate_options = translate_options.replace(f"-of {format}", "")
            translate_options += " -of GTiff"
            translate_output = "{}_translated.tif".format(target.rsplit(".", 1)[0])

        # Log out basic settings and translate source
        log.info(
            f"""Translating to: {translate_output}
    NoData: {nodata}
    Scale: {scale}
    Size: {band.XSize} x {band.YSize}
    Options: {translate_options}
        """
        )
        log.info(f">> gdal_translate {translate_options} {source} {translate_output}")

        translate_ds = gdal.Translate(
            translate_output, source_ds, options=translate_options.replace("-alpha", "")
        )

        if color_table:
            if isfile(color_table) and access(color_table, R_OK):
                # https://gdal.org/python/osgeo.gdal-module.html#DEMProcessingOptions

                if color_table.endswith(".sld"):
                    cpt_file = color_table.replace(".sld", ".cpt")
                    log.info(f"Transforming color table file from sld to cpt: Saving it at {cpt_file}")

                    if not os.path.exists(cpt_file):
                        color_map = ColorMap.from_sld(color_table)
                        color_map.to_cpt(cpt_file)
                    color_table = cpt_file

                if color_options:
                    dem_options = color_options
                else:
                    dem_options = ""
                dem_options += f" -of {format}"

                processing_options = gdal.DEMProcessingOptions(options=dem_options, colorFilename=color_table)

                # Log out basic settings and translate source
                log.info(f"Coloring to: {target}")
                log.info("  Colortable: {}\n  Options: {}".format(color_table, dem_options))
                log.info(f">> gdaldem color-relief {translate_output} {color_table} {target} {dem_options}")
                gdal.DEMProcessing(
                    target,
                    translate_ds,
                    processing="color-relief",
                    options=processing_options,
                )
            else:
                log.error(f"Unable to read colortable: {color_table}")
                sys.exit(1)
        else:
            log.info("No colortable provided, skipping rendering..")

    except Exception as e:
        log.error("Error during quicklook generation: {}".format(e))
        raise Exception("Error during quicklook generation: {}".format(e))

    finally:
        del source_ds, translate_ds
        if os.path.exists(translate_output) and color_table:
            os.remove(translate_output)

        # Restore previous configuration.
        if config_options:
            for key, val in prev_config_options.items():
                gdal.SetConfigOption(key, val)


def get_option(
    options: str,
    option_key: str,
    dictionary: dict = None,
    dictionary_key: str = None,
    exception_message: str = None,
) -> str:
    key = None
    value = None
    if options:
        tokens = options.split(" ")
        for token in tokens:
            is_digit = token.lstrip("-+").isdigit()
            if token.startswith("-") and not is_digit:
                if key:
                    # stop if key already found
                    break
                if token[1:] == option_key:
                    key = token[1:]
            else:
                if value and is_digit and key:
                    value += f" {token}"
                elif not value and key:
                    value = f"{token}"
        if value:
            log.info(f"Option: {key} = {value}")
            return value
        elif exception_message:
            raise Exception(exception_message)

    if dictionary:
        log.debug(f"Dictionary: {dictionary}")
        if isinstance(dictionary, str):
            dictionary = json.loads(dictionary)

        if dictionary.get(dictionary_key):
            log.info(f"Dict: {dictionary_key} = {dictionary.get(dictionary_key)}")
            return dictionary.get(dictionary_key)

    if value is None and exception_message:
        raise Exception(exception_message)

    return None


def get_extension(output_format: str) -> Union[str, None]:
    """Method to extract file extension from file format.

    Args:
        format [str]: file format to generate extension for

    Returns:
        [str | None]
    """
    driver = gdal.GetDriverByName(output_format)
    if driver:
        meta = driver.GetMetadata()
        ext = meta.get("DMD_EXTENSION")
        return ext
    else:
        return None


def get_extension_from_dataset(dataset: Union[str, gdal.Dataset]) -> Union[str, None]:
    """Method to extract file extension from dataset metadata.

    Args:
        dataset [str, gdal.Dataset]: filename or loaded gdal.Dataset object

    Returns:
        [str | None]
    """
    ds = None
    if isinstance(dataset, gdal.Dataset):
        ds = dataset
    elif isinstance(dataset, str):
        ds = gdal.Open(dataset)
    driver = gdal.GetDriver(ds)
    if driver:
        meta = driver.GetMetadata()
        ext = meta["DMD_EXTENSION"]
        del ds
        return ext
    else:
        return None


def get_EPSG(rast_obj: osgeo.gdal.Dataset) -> int:
    """Method to extract the EPSG code

    Returns the EPSG code from a given input georeferenced image or virtual
    raster gdal object.

    Args:
        rast_obj [osgeo.gdal.Dataset]: gdal Raster Object

    Returns:
        EPSG code as integer
    """
    wkt = rast_obj.GetProjection()
    epsg = wkt2epsg(wkt)
    return epsg


def wkt2epsg(wkt: str) -> int:
    """Transforms a WKT string to an EPSG code.

    From https://gis.stackexchange.com/questions/20298/is-it-possible-to-get-the-epsg-value-from-an-osr-spatialreference-class-using-th  # noqa:E501

    Args:
        wkt [str]: WKT definition

    Returns:
        EPSG code as integer
    """

    p_in = osr.SpatialReference()
    s = p_in.ImportFromWkt(wkt)
    if s == 5:  # invalid WKT
        return None
    if p_in.IsLocal() == 1:  # this is a local definition
        return p_in.ExportToWkt()
    if p_in.IsGeographic() == 1:  # this is a geographic srs
        cstype = "GEOGCS"
    else:  # this is a projected srs
        cstype = "PROJCS"
    an = p_in.GetAuthorityName(cstype)
    ac = p_in.GetAuthorityCode(cstype)
    if an is not None and ac is not None:  # return the EPSG code
        return int(p_in.GetAuthorityCode(cstype))


def find_indices(list_to_check: list, item_to_find: Any) -> list:
    indices = []
    for idx, value in enumerate(list_to_check):
        if value == item_to_find:
            indices.append(idx)
    return indices
