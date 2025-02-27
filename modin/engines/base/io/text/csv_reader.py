from modin.engines.base.io.text.text_file_reader import TextFileReader
from modin.data_management.utils import compute_chunksize
from pandas.io.parsers import _validate_usecols_arg
import pandas
import py
import os
import sys


class CSVReader(TextFileReader):
    @classmethod
    def _skip_header(
        cls,
        f,
        comment=None,
        skiprows=None,
        encoding=None,
        header="infer",
        names=None,
        **kwargs
    ):
        lines_read = 0
        if header is None:
            return lines_read
        elif header == "infer":
            if names is not None:
                return lines_read
            else:
                header = 0
        # Skip lines before the header
        if isinstance(skiprows, int):
            lines_read += skiprows
            for _ in range(skiprows):
                f.readline()
            skiprows = None

        header_lines = header + 1 if isinstance(header, int) else max(header) + 1
        header_lines_skipped = 0
        # Python 2 files use a read-ahead buffer which breaks our use of tell()
        for line in iter(f.readline, ""):
            lines_read += 1
            skip = False
            if not skip and comment is not None:
                if encoding is not None:
                    skip |= line.decode(encoding)[0] == comment
                else:
                    skip |= line.decode()[0] == comment
            if not skip and callable(skiprows):
                skip |= skiprows(lines_read)
            elif not skip and hasattr(skiprows, "__contains__"):
                skip |= lines_read in skiprows

            if not skip:
                header_lines_skipped += 1
                if header_lines_skipped == header_lines:
                    return lines_read
        return lines_read

    @classmethod
    def read(cls, filepath_or_buffer, **kwargs):
        if isinstance(filepath_or_buffer, str):
            if not cls.file_exists(filepath_or_buffer):
                return cls.single_worker_read(filepath_or_buffer, **kwargs)
        elif not isinstance(filepath_or_buffer, py.path.local):
            read_from_pandas = True
            # Pandas read_csv supports pathlib.Path
            try:
                import pathlib

                if isinstance(filepath_or_buffer, pathlib.Path):
                    read_from_pandas = False
            except ImportError:  # pragma: no cover
                pass
            if read_from_pandas:
                return cls.single_worker_read(filepath_or_buffer, **kwargs)
        compression_type = cls.infer_compression(
            filepath_or_buffer, kwargs.get("compression")
        )
        if compression_type is not None:
            if (
                compression_type == "gzip"
                or compression_type == "bz2"
                or compression_type == "xz"
            ):
                kwargs["compression"] = compression_type
            elif (
                compression_type == "zip"
                and sys.version_info[0] == 3
                and sys.version_info[1] >= 7
            ):
                # need python3.7 to .seek and .tell ZipExtFile
                kwargs["compression"] = compression_type
            else:
                return cls.single_worker_read(filepath_or_buffer, **kwargs)

        chunksize = kwargs.get("chunksize")
        if chunksize is not None:
            return cls.single_worker_read(filepath_or_buffer, **kwargs)

        skiprows = kwargs.get("skiprows")
        if skiprows is not None and not isinstance(skiprows, int):
            return cls.single_worker_read(filepath_or_buffer, **kwargs)
        # TODO: replace this by reading lines from file.
        if kwargs.get("nrows") is not None:
            return cls.single_worker_read(filepath_or_buffer, **kwargs)
        names = kwargs.get("names", None)
        index_col = kwargs.get("index_col", None)
        if names is None:
            # For the sake of the empty df, we assume no `index_col` to get the correct
            # column names before we build the index. Because we pass `names` in, this
            # step has to happen without removing the `index_col` otherwise it will not
            # be assigned correctly
            names = pandas.read_csv(
                filepath_or_buffer,
                **dict(kwargs, nrows=0, skipfooter=0, index_col=None)
            ).columns
        empty_pd_df = pandas.read_csv(
            filepath_or_buffer, **dict(kwargs, nrows=0, skipfooter=0)
        )
        column_names = empty_pd_df.columns
        skipfooter = kwargs.get("skipfooter", None)
        skiprows = kwargs.pop("skiprows", None)

        usecols = kwargs.get("usecols", None)
        usecols_md = _validate_usecols_arg(usecols)
        if usecols is not None and usecols_md[1] != "integer":
            del kwargs["usecols"]
            all_cols = pandas.read_csv(
                cls.file_open(filepath_or_buffer, "rb"),
                **dict(kwargs, nrows=0, skipfooter=0)
            ).columns
            usecols = all_cols.get_indexer_for(list(usecols_md[0]))
        parse_dates = kwargs.pop("parse_dates", False)
        partition_kwargs = dict(
            kwargs,
            header=None,
            names=names,
            skipfooter=0,
            skiprows=None,
            parse_dates=parse_dates,
            usecols=usecols,
        )
        with cls.file_open(filepath_or_buffer, "rb", compression_type) as f:
            if kwargs.get("encoding", None) is not None:
                partition_kwargs["skiprows"] = 1
                f.seek(0, os.SEEK_SET)  # Return to beginning of file

            # Skip the header since we already have the header information and skip the
            # rows we are told to skip.
            kwargs["skiprows"] = skiprows
            cls._skip_header(f, **kwargs)
            # Launch tasks to read partitions
            partition_ids = []
            index_ids = []
            dtypes_ids = []
            total_bytes = cls.file_size(f)
            # Max number of partitions available
            from modin.pandas import DEFAULT_NPARTITIONS

            num_partitions = DEFAULT_NPARTITIONS
            # This is the number of splits for the columns
            num_splits = min(len(column_names), num_partitions)
            # This is the chunksize each partition will read
            chunk_size = max(1, (total_bytes - f.tell()) // num_partitions)

            # Metadata
            column_chunksize = compute_chunksize(empty_pd_df, num_splits, axis=1)
            if column_chunksize > len(column_names):
                column_widths = [len(column_names)]
                # This prevents us from unnecessarily serializing a bunch of empty
                # objects.
                num_splits = 1
            else:
                column_widths = [
                    column_chunksize
                    if len(column_names) > (column_chunksize * (i + 1))
                    else 0
                    if len(column_names) < (column_chunksize * i)
                    else len(column_names) - (column_chunksize * i)
                    for i in range(num_splits)
                ]

            while f.tell() < total_bytes:
                args = {
                    "fname": filepath_or_buffer,
                    "num_splits": num_splits,
                    **partition_kwargs,
                }
                partition_id = cls.call_deploy(f, chunk_size, num_splits + 2, args)
                partition_ids.append(partition_id[:-2])
                index_ids.append(partition_id[-2])
                dtypes_ids.append(partition_id[-1])

        # Compute the index based on a sum of the lengths of each partition (by default)
        # or based on the column(s) that were requested.
        if index_col is None:
            row_lengths = cls.materialize(index_ids)
            new_index = pandas.RangeIndex(sum(row_lengths))
        else:
            index_objs = cls.materialize(index_ids)
            row_lengths = [len(o) for o in index_objs]
            new_index = index_objs[0].append(index_objs[1:])
            new_index.name = empty_pd_df.index.name

        # Compute dtypes by getting collecting and combining all of the partitions. The
        # reported dtypes from differing rows can be different based on the inference in
        # the limited data seen by each worker. We use pandas to compute the exact dtype
        # over the whole column for each column. The index is set below.
        dtypes = cls.get_dtypes(dtypes_ids)

        partition_ids = cls.build_partition(partition_ids, row_lengths, column_widths)
        # If parse_dates is present, the column names that we have might not be
        # the same length as the returned column names. If we do need to modify
        # the column names, we remove the old names from the column names and
        # insert the new one at the front of the Index.
        if parse_dates is not None:
            # We have to recompute the column widths if `parse_dates` is set because
            # we are not guaranteed to have the correct information regarding how many
            # columns are on each partition.
            column_widths = None
            # Check if is list of lists
            if isinstance(parse_dates, list) and isinstance(parse_dates[0], list):
                for group in parse_dates:
                    new_col_name = "_".join(group)
                    column_names = column_names.drop(group).insert(0, new_col_name)
            # Check if it is a dictionary
            elif isinstance(parse_dates, dict):
                for new_col_name, group in parse_dates.items():
                    column_names = column_names.drop(group).insert(0, new_col_name)
        # Set the index for the dtypes to the column names
        if isinstance(dtypes, pandas.Series):
            dtypes.index = column_names
        else:
            dtypes = pandas.Series(dtypes, index=column_names)
        new_frame = cls.frame_cls(
            partition_ids,
            new_index,
            column_names,
            row_lengths,
            column_widths,
            dtypes=dtypes,
        )
        new_query_compiler = cls.query_compiler_cls(new_frame)

        if skipfooter:
            new_query_compiler = new_query_compiler.drop(
                new_query_compiler.index[-skipfooter:]
            )
        if kwargs.get("squeeze", False) and len(new_query_compiler.columns) == 1:
            return new_query_compiler[new_query_compiler.columns[0]]
        if index_col is None:
            new_query_compiler._modin_frame._apply_index_objs(axis=0)
        return new_query_compiler
