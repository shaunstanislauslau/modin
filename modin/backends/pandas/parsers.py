import pandas
from pandas.core.dtypes.cast import find_common_type
from pandas.io.common import _infer_compression
from modin.engines.base.io import FileReader
from modin.data_management.utils import split_result_of_axis_func_pandas
from modin.error_message import ErrorMessage
from io import BytesIO


def _split_result_for_readers(axis, num_splits, df):  # pragma: no cover
    """Splits the DataFrame read into smaller DataFrames and handles all edge cases.

    Args:
        axis: Which axis to split over.
        num_splits: The number of splits to create.
        df: The DataFrame after it has been read.

    Returns:
        A list of pandas DataFrames.
    """
    splits = split_result_of_axis_func_pandas(axis, num_splits, df)
    if not isinstance(splits, list):
        splits = [splits]
    return splits


class PandasParser(object):
    @classmethod
    def get_dtypes(cls, dtypes_ids):
        return (
            pandas.concat(cls.materialize(dtypes_ids), axis=1)
            .apply(lambda row: find_common_type(row.values), axis=1)
            .squeeze(axis=0)
        )

    @classmethod
    def single_worker_read(cls, fname, **kwargs):
        ErrorMessage.default_to_pandas("Parameters provided")
        # Use default args for everything
        pandas_frame = cls.parse(fname, **kwargs)
        if isinstance(pandas_frame, pandas.io.parsers.TextFileReader):
            pd_read = pandas_frame.read
            pandas_frame.read = lambda *args, **kwargs: cls.query_compiler_cls.from_pandas(
                pd_read(*args, **kwargs), cls.frame_cls
            )
            return pandas_frame
        return cls.query_compiler_cls.from_pandas(pandas_frame, cls.frame_cls)

    infer_compression = _infer_compression


class PandasCSVParser(PandasParser):
    @staticmethod
    def parse(fname, **kwargs):
        num_splits = kwargs.pop("num_splits", None)
        start = kwargs.pop("start", None)
        end = kwargs.pop("end", None)
        index_col = kwargs.get("index_col", None)
        if start is not None and end is not None:
            # pop "compression" from kwargs because bio is uncompressed
            bio = FileReader.file_open(fname, "rb", kwargs.pop("compression", "infer"))
            if kwargs.pop("encoding", False):
                header = b"" + bio.readline()
            else:
                header = b""
            bio.seek(start)
            to_read = header + bio.read(end - start)
            bio.close()
            pandas_df = pandas.read_csv(BytesIO(to_read), **kwargs)
        else:
            # This only happens when we are reading with only one worker (Default)
            return pandas.read_csv(fname, **kwargs)
        if index_col is not None:
            index = pandas_df.index
        else:
            # The lengths will become the RangeIndex
            index = len(pandas_df)
        return _split_result_for_readers(1, num_splits, pandas_df) + [
            index,
            pandas_df.dtypes,
        ]


class PandasJSONParser(PandasParser):
    @staticmethod
    def parse(fname, **kwargs):
        num_splits = kwargs.pop("num_splits", None)
        start = kwargs.pop("start", None)
        end = kwargs.pop("end", None)
        if start is not None and end is not None:
            # pop "compression" from kwargs because bio is uncompressed
            bio = FileReader.file_open(fname, "rb", kwargs.pop("compression", "infer"))
            bio.seek(start)
            to_read = b"" + bio.read(end - start)
            bio.close()
            columns = kwargs.pop("columns")
            pandas_df = pandas.read_json(BytesIO(to_read), **kwargs)
        else:
            # This only happens when we are reading with only one worker (Default)
            return pandas.read_json(fname, **kwargs)
        if not pandas_df.columns.equals(columns):
            raise NotImplementedError("Columns must be the same across all rows.")
        partition_columns = pandas_df.columns
        return _split_result_for_readers(1, num_splits, pandas_df) + [
            len(pandas_df),
            pandas_df.dtypes,
            partition_columns,
        ]
