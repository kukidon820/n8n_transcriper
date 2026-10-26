"""Ошибки выбора и проверки конвертеров узлов n8n."""


class NodeConversionError(ValueError):
    """Базовая ошибка преобразования отдельного узла."""


class UnsupportedNodeVersionError(NodeConversionError):
    """Для типа и версии узла нет зарегистрированной реализации."""


class UnsupportedNodeFeatureError(UnsupportedNodeVersionError):
    """Тип узла есть, но выбранное языковое/функциональное подмножество не реализовано."""


class UnsupportedNodeConfigurationError(NodeConversionError):
    """Параметры узла выходят за реализованный поднабор поведения."""
