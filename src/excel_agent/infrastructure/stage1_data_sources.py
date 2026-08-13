"""阶段1 XLSX/CSV DataSource实现。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..errors import AppError


@dataclass(frozen=True)
class RawInspection:
    format: str
    objects: list[dict[str, Any]]
    encoding_candidates: list[str]
    delimiter_candidates: list[str]
    validation_errors: list[dict[str, Any]]


def _decode_candidates(path: Path) -> tuple[list[str], dict[str, str]]:
    raw = path.read_bytes()
    candidates: list[str] = []
    decoded: dict[str, str] = {}
    seen_text: set[str] = set()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        normalized = text.lstrip("\ufeff")
        if normalized not in seen_text:
            candidates.append(encoding)
            decoded[encoding] = normalized
            seen_text.add(normalized)
    return candidates, decoded


def _delimiter_candidates(text: str) -> list[str]:
    sample = text[:64 * 1024]
    delimiters = [",", "\t", ";", "|"]
    counts: dict[str, int] = {}
    for delimiter in delimiters:
        rows = [line for line in sample.splitlines()[:20] if line.strip()]
        counts[delimiter] = sum(line.count(delimiter) for line in rows)
    candidates = [delimiter for delimiter in delimiters if counts[delimiter] > 0]
    if len(candidates) <= 1:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
            return [dialect.delimiter]
        except csv.Error:
            return candidates
    return candidates


class DataSource:
    def inspect(self, path: Path) -> RawInspection:  # pragma: no cover - protocol-style base
        raise NotImplementedError

    def load(
        self,
        path: Path,
        *,
        object_name: str | None = None,
        encoding: str | None = None,
        delimiter: str | None = None,
    ) -> pd.DataFrame:  # pragma: no cover - protocol-style base
        raise NotImplementedError


class XlsxDataSource(DataSource):
    def __init__(self, *, max_sheets: int = 10) -> None:
        self.max_sheets = max_sheets

    def inspect(self, path: Path) -> RawInspection:
        try:
            with pd.ExcelFile(path) as excel:
                sheets = list(excel.sheet_names)
        except Exception as exc:
            raise AppError("UPLOAD_PARSE_FAILED", "工作簿解析失败", 422) from exc
        if len(sheets) > self.max_sheets:
            raise AppError(
                "DATASET_SHEET_LIMIT_EXCEEDED",
                "工作簿Sheet数量超过限制",
                413,
                {"actual": len(sheets), "max": self.max_sheets},
            )
        return RawInspection(
            format="xlsx",
            objects=[{"name": sheet, "kind": "sheet"} for sheet in sheets],
            encoding_candidates=[],
            delimiter_candidates=[],
            validation_errors=[],
        )

    def load(
        self,
        path: Path,
        *,
        object_name: str | None = None,
        encoding: str | None = None,
        delimiter: str | None = None,
    ) -> pd.DataFrame:
        inspection = self.inspect(path)
        sheets = [item["name"] for item in inspection.objects]
        selected = object_name
        if selected is None and len(sheets) == 1:
            selected = sheets[0]
        if selected is None or selected not in sheets:
            raise AppError(
                "SHEET_SELECTION_REQUIRED",
                "请选择有效的Excel Sheet",
                422,
                {"objects": inspection.objects},
            )
        try:
            frame = pd.read_excel(path, sheet_name=selected)
        except Exception as exc:
            raise AppError("DATASET_PARSE_FAILED", "Sheet解析失败", 422) from exc
        if frame.empty and len(frame.columns) == 0:
            raise AppError("DATASET_EMPTY", "Dataset为空", 422)
        return frame


class CsvDataSource(DataSource):
    _default_encoding = "utf-8-sig"

    def inspect(self, path: Path) -> RawInspection:
        encodings, decoded = _decode_candidates(path)
        if not encodings:
            raise AppError("CSV_ENCODING_UNSUPPORTED", "无法识别CSV编码", 422)
        delimiters = _delimiter_candidates(decoded[encodings[0]])
        errors: list[dict[str, Any]] = []
        if len(encodings) > 1:
            errors.append(
                {
                    "code": "CSV_ENCODING_AMBIGUOUS",
                    "message": "CSV编码存在多个候选，请确认编码",
                    "candidates": encodings,
                }
            )
        if len(delimiters) > 1:
            errors.append(
                {
                    "code": "CSV_DELIMITER_AMBIGUOUS",
                    "message": "CSV分隔符存在多个候选，请确认分隔符",
                    "candidates": delimiters,
                }
            )
        return RawInspection(
            format="csv",
            objects=[{"name": path.name, "kind": "csv"}],
            encoding_candidates=encodings,
            delimiter_candidates=delimiters,
            validation_errors=errors,
        )

    def load(
        self,
        path: Path,
        *,
        object_name: str | None = None,
        encoding: str | None = None,
        delimiter: str | None = None,
    ) -> pd.DataFrame:
        inspection = self.inspect(path)
        selected_encoding = encoding or (
            inspection.encoding_candidates[0]
            if len(inspection.encoding_candidates) == 1
            else None
        )
        selected_delimiter = delimiter or (
            inspection.delimiter_candidates[0]
            if len(inspection.delimiter_candidates) == 1
            else None
        )
        if selected_encoding is None:
            raise AppError(
                "CSV_ENCODING_CONFIRMATION_REQUIRED",
                "请确认CSV编码",
                422,
                {"candidates": inspection.encoding_candidates},
            )
        if selected_delimiter is None:
            raise AppError(
                "CSV_DELIMITER_CONFIRMATION_REQUIRED",
                "请确认CSV分隔符",
                422,
                {"candidates": inspection.delimiter_candidates},
            )
        if selected_encoding not in inspection.encoding_candidates:
            raise AppError("CSV_ENCODING_INVALID", "CSV编码不在候选范围内", 422)
        if selected_delimiter not in inspection.delimiter_candidates:
            raise AppError("CSV_DELIMITER_INVALID", "CSV分隔符不在候选范围内", 422)
        try:
            frame = pd.read_csv(
                path,
                encoding=selected_encoding,
                sep=selected_delimiter,
                dtype=object,
            )
        except (UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            raise AppError("DATASET_PARSE_FAILED", "CSV解析失败", 422) from exc
        if frame.empty and len(frame.columns) == 0:
            raise AppError("DATASET_EMPTY", "Dataset为空", 422)
        return frame


def data_source_for_suffix(suffix: str, *, max_sheets: int = 10) -> DataSource:
    normalized = suffix.lower()
    if normalized == ".xlsx":
        return XlsxDataSource(max_sheets=max_sheets)
    if normalized == ".csv":
        return CsvDataSource()
    raise AppError("UPLOAD_FORMAT_UNSUPPORTED", "不支持的文件格式", 400)
