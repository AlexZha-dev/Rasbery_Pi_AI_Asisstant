class LCDException(Exception):
    """Базовое исключение для всех LCD ошибок"""

    pass


class LCDCoordinateError(LCDException):
    """Ошибка при неверных координатах"""

    pass


class LCDRowError(LCDException):
    """Ошибка при неверной строке"""

    pass


class LCDScrollDirectionError(LCDException):
    """Ошибка при неверном направлении прокрутки"""

    pass
