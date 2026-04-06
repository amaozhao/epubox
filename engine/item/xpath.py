import re

# e.g. html/body/div[0]:text:0  or  html/body/p[1]:attr:3
XPATH_PATTERN = re.compile(r"^[a-zA-Z0-9/_.\-]+\[\d+\]:(?:text|attr):\d+$")


def is_valid_xpath(xpath: str) -> bool:
    """Return True if xpath matches the expected pattern."""
    return bool(XPATH_PATTERN.match(xpath))


def parse_xpath(xpath: str) -> tuple[str, str, int]:
    """
    Parse an xpath string into its components.

    Returns:
        tuple of (path_prefix, type, index)
        - path_prefix: the element path, e.g. "html/body/div[0]"
        - type: "text" or "attr"
        - index: integer position

    Raises:
        ValueError: if xpath is not valid
    """
    if not is_valid_xpath(xpath):
        raise ValueError(f"Invalid xpath: {xpath}")

    # Split from the right: path_prefix : type : index
    last_colon = xpath.rindex(":")
    index = int(xpath[last_colon + 1:])

    second_colon = xpath.rindex(":", 0, last_colon)
    xpath_type = xpath[second_colon + 1:last_colon]

    path_prefix = xpath[:second_colon]

    return path_prefix, xpath_type, index
