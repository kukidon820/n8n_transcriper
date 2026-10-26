"""Ограниченные ZIP/GZIP и Extract from File runtime-адаптеры."""

import base64
import csv
import gzip
import io
import json
import zipfile
from typing import Literal

from pydantic import Field
from pypdf import PdfReader

from ..domain.models.workflow import WorkflowNode
from ..runtime.executors import NodeExecutionContext
from ..runtime.models import NodeResult, WorkflowItem
from .errors import UnsupportedNodeConfigurationError
from .nodes import EmptyOptions, _StrictParameters

_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_CSV_ROWS = 50_000


class CompressionParameters(_StrictParameters):
    """Поддерживаемые настройки ZIP/GZIP узла Compression."""

    operation: Literal["compress", "decompress"]
    binary_property_name: str = Field(default="data", alias="binaryPropertyName")
    output_format: Literal["zip", "gzip"] = Field(default="zip", alias="outputFormat")
    file_name: str = Field(default="data", alias="fileName", min_length=1)
    binary_property_output: str = Field(default="data", alias="binaryPropertyOutput")
    output_prefix: str = Field(default="file", alias="outputPrefix")
    options: EmptyOptions = Field(default_factory=EmptyOptions)


class ExtractOptions(_StrictParameters):
    """Параметры форматов, которые поддерживает extraction-адаптер."""

    encoding: Literal["utf-8", "utf-8-sig", "latin-1"] = "utf-8-sig"
    strip_bom: bool = Field(default=True, alias="stripBOM")
    keep_source: bool = Field(default=True, alias="keepSource")
    header_row: bool = Field(default=True, alias="headerRow")
    include_empty_cells: bool = Field(default=True, alias="includeEmptyCells")
    join_pages: bool = Field(default=True, alias="joinPages")
    max_pages: int = Field(default=500, alias="maxPages", gt=0, le=500, strict=True)


class ExtractFileParameters(_StrictParameters):
    """Настройки извлечения CSV, JSON или PDF из binary-поля item."""

    operation: Literal["csv", "fromJson", "pdf"]
    binary_property_name: str = Field(default="data", alias="binaryPropertyName")
    destination_key: str = Field(default="data", alias="destinationKey")
    options: ExtractOptions = Field(default_factory=ExtractOptions)


def _binary_bytes(item: WorkflowItem, property_name: str) -> tuple[bytes, dict[str, object]]:
    """Прочитать n8n-совместимое base64 binary-поле и проверить размер."""
    binary = (item.binary or {}).get(property_name)
    if not isinstance(binary, dict) or not isinstance(binary.get("data"), str):
        raise UnsupportedNodeConfigurationError(
            f"Входной item не содержит binary-поле {property_name!r} с base64-данными."
        )
    try:
        content = base64.b64decode(binary["data"], validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise UnsupportedNodeConfigurationError(
            f"Binary-поле {property_name!r} содержит некорректный base64."
        ) from error
    if len(content) > _MAX_FILE_BYTES:
        raise UnsupportedNodeConfigurationError("Размер входного файла превышает 50 MiB.")
    return content, binary


def _binary_metadata(content: bytes, name: str, mime_type: str) -> dict[str, object]:
    """Сформировать inline binary-значение для сериализуемого WorkflowItem."""
    return {
        "data": base64.b64encode(content).decode("ascii"),
        "fileName": name,
        "fileExtension": name.rsplit(".", 1)[-1] if "." in name else "",
        "mimeType": mime_type,
    }


class CompressionExecutor:
    """Сжимать или распаковывать один binary-файл без записи на диск."""

    is_trigger = False

    def __init__(self, parameters: CompressionParameters):
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        output: list[WorkflowItem] = []
        for item in items:
            content, _ = _binary_bytes(item, self._parameters.binary_property_name)
            if self._parameters.operation == "compress":
                archive = self._compress(content)
                name = self._compressed_name()
                mimetype = (
                    "application/zip"
                    if self._parameters.output_format == "zip"
                    else "application/gzip"
                )
                output.append(
                    _replace_binary(
                        item,
                        self._parameters.binary_property_output,
                        _binary_metadata(archive, name, mimetype),
                    )
                )
            else:
                for extracted_name, extracted, mime_type in self._decompress(content):
                    output_property = (
                        self._parameters.output_prefix
                        if self._parameters.output_format == "gzip"
                        else f"{self._parameters.output_prefix}{extracted_name}"
                    )
                    output.append(
                        _replace_binary(
                            item,
                            output_property,
                            _binary_metadata(extracted, extracted_name, mime_type),
                        )
                    )
        return NodeResult(outputs=(tuple(output),))

    def _compressed_name(self) -> str:
        name = self._parameters.file_name
        if self._parameters.output_format == "gzip" and not name.endswith(".gz"):
            return f"{name}.gz"
        if self._parameters.output_format == "zip" and not name.endswith(".zip"):
            return f"{name}.zip"
        return name

    def _compress(self, content: bytes) -> bytes:
        if self._parameters.output_format == "gzip":
            return gzip.compress(content)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self._parameters.file_name, content)
        return buffer.getvalue()

    def _decompress(self, content: bytes) -> list[tuple[str, bytes, str]]:
        if self._parameters.output_format == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                    extracted = stream.read(_MAX_FILE_BYTES + 1)
            except (OSError, EOFError) as error:
                raise UnsupportedNodeConfigurationError(
                    "Не удалось распаковать GZIP-файл."
                ) from error
            if len(extracted) > _MAX_FILE_BYTES:
                raise UnsupportedNodeConfigurationError("Распакованный файл превышает 50 MiB.")
            return [(self._gzip_output_name(), extracted, "application/octet-stream")]

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                files = [entry for entry in archive.infolist() if not entry.is_dir()]
                if not files:
                    raise UnsupportedNodeConfigurationError("ZIP-архив не содержит файлов.")
                extracted_files: list[tuple[str, bytes, str]] = []
                total_size = 0
                for entry in files:
                    total_size += entry.file_size
                    if total_size > _MAX_FILE_BYTES:
                        raise UnsupportedNodeConfigurationError(
                            "Общий размер распакованных файлов превышает 50 MiB."
                        )
                    with archive.open(entry) as stream:
                        data = stream.read(_MAX_FILE_BYTES + 1)
                    if len(data) != entry.file_size or len(data) > _MAX_FILE_BYTES:
                        raise UnsupportedNodeConfigurationError(
                            "ZIP-архив содержит некорректный или слишком большой файл."
                        )
                    extracted_files.append(
                        (entry.filename.rsplit("/", 1)[-1], data, "application/octet-stream")
                    )
                return extracted_files
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise UnsupportedNodeConfigurationError("Не удалось распаковать ZIP-файл.") from error

    def _gzip_output_name(self) -> str:
        return self._parameters.file_name.removesuffix(".gz")


class ExtractFromFileExecutor:
    """Преобразовать binary-данные в JSON items для CSV, JSON или PDF."""

    is_trigger = False

    def __init__(self, parameters: ExtractFileParameters):
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        output: list[WorkflowItem] = []
        for item in items:
            content, _ = _binary_bytes(item, self._parameters.binary_property_name)
            if self._parameters.operation == "csv":
                records = self._extract_csv(content)
            elif self._parameters.operation == "fromJson":
                records = self._extract_json(content)
            else:
                records = self._extract_pdf(content)
            for record in records:
                output.append(
                    WorkflowItem(
                        json={self._parameters.destination_key: record},
                        binary=item.binary if self._parameters.options.keep_source else None,
                    )
                )
        return NodeResult(outputs=(tuple(output),))

    def _extract_csv(self, content: bytes) -> list[dict[str, object]]:
        try:
            encoding = self._parameters.options.encoding
            if encoding == "utf-8-sig" and not self._parameters.options.strip_bom:
                encoding = "utf-8"
            text = content.decode(encoding)
            if self._parameters.options.header_row:
                reader = csv.DictReader(io.StringIO(text))
                rows = [dict(row) for row in reader]
                if not self._parameters.options.include_empty_cells:
                    rows = [
                        {key: value for key, value in row.items() if value not in (None, "")}
                        for row in rows
                    ]
            else:
                rows = [
                    {str(column): value for column, value in enumerate(row)}
                    for row in csv.reader(io.StringIO(text))
                ]
        except (UnicodeDecodeError, csv.Error) as error:
            raise UnsupportedNodeConfigurationError("Не удалось декодировать CSV.") from error
        if len(rows) > _MAX_CSV_ROWS:
            raise UnsupportedNodeConfigurationError("CSV содержит больше 50 000 строк.")
        if any(None in row for row in rows):
            raise UnsupportedNodeConfigurationError("В CSV есть строка с лишними столбцами.")
        return rows

    @staticmethod
    def _extract_json(content: bytes) -> list[object]:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UnsupportedNodeConfigurationError(
                "Binary-файл не содержит корректный JSON."
            ) from error
        values = value if isinstance(value, list) else [value]
        if any(
            not isinstance(row, (dict, list, str, int, float, bool, type(None))) for row in values
        ):
            raise UnsupportedNodeConfigurationError("JSON содержит неподдерживаемый тип значения.")
        return values

    def _extract_pdf(self, content: bytes) -> list[str]:
        try:
            reader = PdfReader(io.BytesIO(content))
            page_count = len(reader.pages)
            pages = [
                page.extract_text() or ""
                for page in reader.pages[: self._parameters.options.max_pages]
            ]
        except Exception as error:
            raise UnsupportedNodeConfigurationError("Не удалось извлечь текст из PDF.") from error
        if page_count > self._parameters.options.max_pages:
            raise UnsupportedNodeConfigurationError(
                f"PDF превышает лимит {self._parameters.options.max_pages} страниц."
            )
        if self._parameters.options.join_pages:
            return ["\n".join(pages)]
        return pages


def _replace_binary(
    item: WorkflowItem,
    property_name: str,
    binary_value: dict[str, object],
) -> WorkflowItem:
    """Создать item с прежним JSON и добавленным/заменённым binary-полем."""
    binary = dict(item.binary or {})
    binary[property_name] = binary_value
    return WorkflowItem(json=item.json_data, binary=binary)
