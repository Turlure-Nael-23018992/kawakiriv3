"""Repairs a CSV export into a standard CSV DuckDB can read.

Handles the two defects found in the JD Edwards F03B11 extract
(examples/jde_f03b11/raw/output.csv):

1. a stray terminator at the end of every line (`...,2026-03-17 10:08:27.440;`),
   header included -- stripped from the last field;
2. rows the exporter wrapped ENTIRELY in quotes, inner quotes doubled
   (`"a,b,""ZWS14,5%"",c;"`) -- the whole row arrives as one field, which is
   itself a CSV line: it is parsed a second time.

Any row that still doesn't have as many fields as the header after that is
reported and left out, never silently padded.

    python scripts/clean_csv_export.py <input.csv> <output.csv> [--terminator ";"]
"""
import argparse
import csv
import io
import sys


def _stripTerminator(fields, terminator):
    if terminator and fields and fields[-1].endswith(terminator):
        fields[-1] = fields[-1][: -len(terminator)]
    return fields


def clean(inputPath, outputPath, terminator=";", encoding="utf-8"):
    """Returns `(rowsWritten, rowsUnwrapped, rejectedLineNumbers)`."""
    rowsWritten = rowsUnwrapped = 0
    rejected = []
    with open(inputPath, encoding=encoding, newline="") as source, \
            open(outputPath, "w", encoding="utf-8", newline="") as target:
        reader = csv.reader(source)
        writer = csv.writer(target)
        header = _stripTerminator(next(reader), terminator)
        writer.writerow(header)

        for fields in reader:
            if len(fields) == 1 and len(header) > 1:
                fields = next(csv.reader(io.StringIO(fields[0])))
                rowsUnwrapped += 1
            fields = _stripTerminator(fields, terminator)
            if len(fields) != len(header):
                rejected.append(reader.line_num)
                continue
            writer.writerow(fields)
            rowsWritten += 1
    return rowsWritten, rowsUnwrapped, rejected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--terminator", default=";", help="caractere parasite en fin de ligne ('' pour aucun)")
    args = parser.parse_args()

    written, unwrapped, rejected = clean(args.input, args.output, args.terminator)
    print(f"{written} lignes ecrites, dont {unwrapped} desencapsulees ; {len(rejected)} rejetees")
    if rejected:
        print(f"lignes rejetees (numero de ligne source) : {rejected[:20]}", file=sys.stderr)
