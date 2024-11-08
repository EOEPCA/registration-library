import logging
import re
from string import Template

import webcolors
from bs4 import BeautifulSoup as bs

log = logging.getLogger("oseostac")


def hex_to_rgb(hexa):
    return tuple(int(hexa[i: i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(r, g, b):
    return "#{0:02x}{1:02x}{2:02x}".format(clamp(r), clamp(g), clamp(b))


def clamp(x):
    return max(0, min(x, 255))


class ColorMapEntry(object):
    value: float
    red: int
    green: int
    blue: int
    opacity: int
    label: str

    def __init__(self, value: float, r: int, g: int, b: int, opacity: float = None, label: str = None):
        self.value = value
        self.red = r
        self.green = g
        self.blue = b
        self.opacity = opacity
        self.label = label

    def has_value(self):
        return isinstance(self.value, float)

    @staticmethod
    def from_hex(value: float, hexa: str, opacity: float = None, label: str = None):
        if hexa.startswith("#"):
            hexa = hexa[1:]
        r, g, b = hex_to_rgb(hexa)
        return ColorMapEntry(value=value, r=r, g=g, b=b, opacity=opacity, label=label)

    @staticmethod
    def from_rgb(self, value: float, r: int, g: int, b: int, opacity: float = None, label: str = None):
        return ColorMapEntry(value=value, r=r, g=g, b=b, opacity=opacity, label=label)

    def color_as_hex(self):
        return rgb_to_hex(self.red, self.green, self.blue)

    def color_as_tuple(self):
        return tuple([self.red, self.green, self.blue])

    def __repr__(self):
        return (
            f"ColorMapEntry [{self.value}]: "
            f"R={self.red},"
            f"G={self.green},"
            f"B={self.blue}, "
            f"Opacity={self.opacity}, "
            f"Label={self.label}"
        )


class ColorMap(object):
    entries: [ColorMapEntry]
    bg_color: ColorMapEntry
    fg_color: ColorMapEntry
    nan_color: ColorMapEntry

    """A class to read, write and convert color information from different formats
    """

    def __init__(
        self,
        entries: [ColorMapEntry],
        bg_color: ColorMapEntry = None,
        fg_color: ColorMapEntry = None,
        nan_color: ColorMapEntry = None,
    ):
        self.entries = entries
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.nan_color = nan_color

    def __repr__(self):
        r = f"ColorMap [{len(self.entries)}]:\n"
        for entry in self.entries:
            r += (
                f"  Value[{entry.value}], "
                f"RGB[{entry.red},{entry.green},{entry.blue}], "
                f"Opacity[{entry.opacity}], "
                f"Label[{entry.label}]\n"
            )
        if self.bg_color:
            r += f"  Background[{self.bg_color.red},{self.bg_color.green},{self.bg_color.blue}]\n"
        if self.fg_color:
            r += f"  Foreground[{self.fg_color.red},{self.fg_color.green},{self.fg_color.blue}]\n"
        if self.nan_color:
            r += f"  NoData[{self.nan_color.red},{self.nan_color.green},{self.nan_color.blue}]\n"
        return r

    @staticmethod
    def _parse_cpt_line(line: str, first_color=True):
        line_tokens = line.split()

        # determine token index for first and second "value"
        #
        # 4 tokens: ['N', '0', '0', '0']
        # 4 tokens: ['0.125', '31/40/79', '0.25', '38/60/106']
        # 4 tokens: ['0', 'black', '0.125', 'red']
        # 6 tokens: ['0', 'black', '0.125', '31', '40', '79']
        # 8 tokens: ['0', '31', '40', '79', '0.125', '31', '40', '79']
        first_value_idx = 0
        if len(line_tokens) == 4 and "/" in line_tokens[1]:
            second_value_idx = 2
        elif len(line_tokens) == 4:
            second_value_idx = 2
        elif len(line_tokens) == 6:
            second_value_idx = 2
        elif len(line_tokens) == 8:
            second_value_idx = 4

        # select index based on first_color flag
        if first_color:
            value_idx = first_value_idx
            color_idx = first_value_idx + 1
        else:
            value_idx = second_value_idx
            color_idx = second_value_idx + 1

        # read value or set corresponding flags
        try:
            value = float(line_tokens[value_idx])
            is_value = True
        except Exception as e:
            log.warning(e)
            value = None
            is_value = False

        # skip B,F,N as does not have a second color block
        if not first_color and not is_value:
            return None

        # read colors respecting different formats
        if not line_tokens[color_idx].strip().isdecimal() and "/" not in line_tokens[1]:
            # the entry may contain named colors e.g.
            # ['0', 'black', '0.125', 'red']
            # ['0', 'black', '0.125', '31', '40', '79']
            # we need to replace black and the following element
            # and insert a third to replace the named string
            r, g, b = webcolors.name_to_rgb(line_tokens[color_idx].strip())
        elif "/" in line_tokens[color_idx]:
            # rgb color is separated by "/"
            # ['0.125', '31/40/79', '0.25', '38/60/106']
            slash_tokens = line_tokens[color_idx].strip().split("/")
            r = slash_tokens[0]
            g = slash_tokens[1]
            b = slash_tokens[2]
        else:
            # a nominal block contains an array like this:
            # ['0', '31', '40', '79', '0.125', '31', '40', '79']
            r = line_tokens[color_idx]
            g = line_tokens[color_idx + 1]
            b = line_tokens[color_idx + 2]

        # build and return the ColorMapEntry
        return ColorMapEntry(value=value, r=int(r), g=int(g), b=int(b))

    @staticmethod
    def from_cpt(input_file):
        entries = []

        log.info(f"Reading CPT from: {input_file}")
        with open(input_file) as cpt_file:
            lines = cpt_file.readlines()

        # first, read all the lines with values and colors, e.g. "0 R/G/B 1 R/G/B"
        non_value_lines = ["B", "F", "N", "#"]
        last_value_line = None
        for line in lines:
            # Skip all lines starting with B,N,F and #
            if any([x in line[0] for x in non_value_lines]):
                continue

            # save line for later, parse color and store in entries
            last_value_line = line.replace("\n", "")
            color_map_entry = ColorMap._parse_cpt_line(last_value_line)
            entries.append(color_map_entry)

        # reread the last line to capture the second color block
        color_map_entry = ColorMap._parse_cpt_line(last_value_line, first_color=False)
        entries.append(color_map_entry)

        # now read the non-value lines and append to entries
        bg_color = None
        fg_color = None
        nan_color = None
        for line in lines:
            if any([x in line[0] for x in non_value_lines]):
                line = line.replace("\n", "")

                if line[0] == "B":
                    bg_color = ColorMap._parse_cpt_line(line)
                    continue

                if line[0] == "F":
                    fg_color = ColorMap._parse_cpt_line(line)
                    continue

                if line[0] == "N":
                    nan_color = ColorMap._parse_cpt_line(line)
                    continue

        return ColorMap(entries=entries, bg_color=bg_color, fg_color=fg_color, nan_color=nan_color)

    @staticmethod
    def from_geocss(input_file):
        entries = []

        log.info(f"Reading GeoCSS from: {input_file}")
        lines = ""
        with open(input_file) as f:
            lines = f.read()

        color_entries = re.findall(r"color-map-entry\(.*?\)", lines)
        for color_entry in color_entries:
            values = color_entry.replace("color-map-entry(", "").replace(")", "").replace('"', "").split(",")

            opacity = None
            label = None
            value = None
            if len(values) > 1:
                value = values[1].strip()
            if len(values) > 2:
                opacity = values[2].strip()
            if len(values) > 3:
                label = values[3].strip()

            entry = ColorMapEntry.from_hex(hexa=values[0], value=value, opacity=opacity, label=label)
            log.info(entry)
            entries.append(entry)

        return ColorMap(entries)

    @staticmethod
    def from_sld(input_file):
        entries = []
        nan_color = None
        bg_color = None
        fg_color = None

        log.info(f"Reading SLD from: {input_file}")

        with open(input_file, "r", encoding="cp1252") as f:
            data = f.read()
            bs_data = bs(data, "xml")

        color_entries = bs_data.find_all(["ColorMapEntry", "sld:ColorMapEntry"])

        for color_entry in color_entries:
            nan_values = ["No Data", "NaN", "nan", "nodata"]
            if color_entry.get("label") in nan_values:
                nan_color = ColorMapEntry.from_hex(
                    hexa=color_entry.get("color"),
                    value=color_entry.get("quantity"),
                    opacity=color_entry.get("opacity", None),
                    label=color_entry.get("label", None),
                )
            elif color_entry.get("label") == "Background":
                bg_color = ColorMapEntry.from_hex(
                    hexa=color_entry.get("color"),
                    value=color_entry.get("quantity"),
                    opacity=color_entry.get("opacity", None),
                    label=color_entry.get("label", None),
                )
            elif color_entry.get("label") == "Foreground":
                fg_color = ColorMapEntry.from_hex(
                    hexa=color_entry.get("color"),
                    value=color_entry.get("quantity"),
                    opacity=color_entry.get("opacity", None),
                    label=color_entry.get("label", None),
                )
            else:
                entry = ColorMapEntry.from_hex(
                    hexa=color_entry.get("color"),
                    value=color_entry.get("quantity"),
                    opacity=color_entry.get("opacity", None),
                    label=color_entry.get("label", None),
                )
                log.info(entry)
                entries.append(entry)

        return ColorMap(entries, nan_color=nan_color, bg_color=bg_color, fg_color=fg_color)

    def to_geocss(
        self,
        title: str = "Default Title",
        description: str = "Default Description",
        # https://docs.geoserver.org/latest/en/user/styling/sld/reference/rastersymbolizer.html#type
        color_map_type: str = "ramp",
        raster_channels: str = "auto",
        label_template: str = "$value",
        with_opacity: bool = False,
        with_labels: bool = False,
        with_info_label: bool = False,
        info_label: str = None,
        output_file: str = None,
    ):
        # Generate GeoCSS
        geocss = "/*\n"
        if title:
            geocss += f"* @title {title}\n"
        if description:
            geocss += f"* @abstract {description}\n"
        geocss += "*/\n\n"
        geocss += "* {\n"
        if raster_channels:
            geocss += f"  raster-channels: {raster_channels};\n"
        if with_info_label and info_label:
            geocss += "  raster-label-fi: add;\n"
            geocss += f'  raster-label-name: "{info_label}";\n'
        if color_map_type:
            geocss += f"  raster-color-map-type: {color_map_type};\n"
        geocss += "  raster-color-map:\n"
        for entry in self.entries:
            if entry.has_value():
                geocss += f"    color-map-entry({entry.color_as_hex()}, {entry.value}"
                if entry.opacity or with_opacity:
                    if entry.opacity:
                        geocss += f", {entry.opacity}"
                    else:
                        geocss += ", 1.0"
                if with_labels:
                    if label_template:
                        label = Template(label_template).safe_substitute(value=entry.value)
                        geocss += f', "{label}"'
                    else:
                        geocss += f', "{entry.label}"'
                geocss += ")\n"

        geocss += "}\n"

        # Write GeoCSS to file
        if output_file:
            log.info(f"Writing GeoCSS to: {output_file}")
            with open(output_file, "w") as f:
                f.write(geocss)
            return None
        else:
            return geocss

    def to_sld(
        self,
        title: str = "Default Title",
        description: str = "Default Description",
        color_map_type: str = "ramp",
        label_template: str = "$value",
        with_opacity: bool = False,
        with_labels: bool = False,
        output_file: str = None,
    ):
        extended = "false"
        if len(self.entries) > 255:
            extended = "true"

        sld = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
"""  # noqa: E501
        sld += "  <NamedLayer>\n"
        sld += "    <Name></Name>\n"
        sld += "    <UserStyle>\n"
        sld += f"      <Title>{title}</Title>\n"
        sld += f"      <Abstract>{description}</Abstract>\n"
        sld += "      <FeatureTypeStyle>\n"
        sld += "         <Rule>\n"
        sld += "             <RasterSymbolizer>\n"
        sld += "                 <Opacity>1.0</Opacity>\n"
        sld += f'                 <ColorMap type="{color_map_type}" extended="{extended}">\n'

        for entry in self.entries:
            sld += (
                f'                     <ColorMapEntry color="{entry.color_as_hex()}" quantity="{entry.value}"'
            )
            if entry.opacity or with_opacity:
                if entry.opacity:
                    sld += f' opacity="{entry.opacity}"'
                else:
                    sld += ' opacity="1.0"'
            if with_labels:
                if label_template:
                    label = Template(label_template).safe_substitute(value=entry.value)
                    sld += f' label="{label}"'
                else:
                    sld += f' label="{entry.label}"'
            sld += "/>\n"

        sld += "                 </ColorMap>\n"
        sld += "             </RasterSymbolizer>\n"
        sld += "         </Rule>\n"
        sld += "      </FeatureTypeStyle>\n"
        sld += "    </UserStyle>\n"
        sld += "  </NamedLayer>\n"
        sld += "</StyledLayerDescriptor>\n"

        # Write SLD to file
        if output_file:
            log.info(f"Writing SLD to: {output_file}")
            with open(output_file, "w") as f:
                f.write(sld)
            return None
        else:
            return sld

    def to_cpt(
        self, title: str = "Default Title", description: str = "Default Description", output_file: str = None
    ):
        cpt = ""

        if title:
            cpt += f"# Title: {title}\n"
        if description:
            cpt += f"# Description: {description}\n"

        for i in range(len(self.entries)):
            entry = self.entries[i]

            if i >= len(self.entries) - 1:
                break

            lower_value = entry.value
            lower_color_r = entry.red
            lower_color_g = entry.green
            lower_color_b = entry.blue

            upper_value = self.entries[i + 1].value
            upper_color_r = self.entries[i + 1].red
            upper_color_g = self.entries[i + 1].green
            upper_color_b = self.entries[i + 1].blue

            cpt += (
                f"{lower_value}\t{int(lower_color_r)}\t{int(lower_color_g)}\t{int(lower_color_b)}"
                f"\t{upper_value}\t{int(upper_color_r)}\t{int(upper_color_g)}\t{int(upper_color_b)}\n"
            )

        if self.bg_color:
            cpt += f"B\t{self.bg_color.red}\t{self.bg_color.green}\t{self.bg_color.blue}\n"
        if self.fg_color:
            cpt += f"F\t{self.fg_color.red}\t{self.fg_color.green}\t{self.fg_color.blue}\n"
        if self.nan_color:
            cpt += f"N\t{self.nan_color.red}\t{self.nan_color.green}\t{self.nan_color.blue}\n"

        if output_file:
            log.info(f"Writing CPT to: {output_file}")
            with open(output_file, "w") as f:
                f.write(cpt)
        else:
            return cpt
