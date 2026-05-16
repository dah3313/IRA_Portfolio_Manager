"""
markdown_tables.py — Markdown-with-tab-separated-tables writer.

Public exports:
    write_md_table(file, columns, rows) — write a Markdown table with
        tab separators between cells

See also: IPMS_SPECIFICATION.md §5.3 (Markdown-with-tabs convention)


WHY MARKDOWN + TABS

Per spec §5.3, file output uses Markdown's pipe-table syntax with
TAB separators between cells (rather than spaces). This unusual
choice gives:
  - Renders as proper aligned table in any Markdown viewer (VS Code
    preview, GitHub, Obsidian, etc.) because the pipe-table
    semantics handle column alignment automatically.
  - Reads cleanly as raw text in any monospace editor — tabs are
    wider than spaces, so columns align visually without padding.
  - Parses cleanly in pandas via pd.read_csv(path, sep='\\t')
    after stripping the `|` pipe characters.
  - Diffs cleanly across simulator versions — each row is a single
    line, column structure is stable.

Format specifics:
  Header row:    | col1\\t| col2\\t| col3 |
  Separator:     |---\\t|---\\t|--- |
  Data rows:     | val1\\t| val2\\t| val3 |

Note: Markdown's strict pipe-table syntax requires the separator
row's `---` markers be properly spaced. We use the minimum form
`---` rather than `:---:` (which forces center alignment) because
it lets the renderer pick its own alignment based on content type.
"""

from __future__ import annotations

from typing import Sequence, TextIO


def write_md_table(
    file: TextIO,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> None:
    """
    Write a Markdown table with tab-separated cells to `file`.

    `columns` is the header row — list of column names.
    `rows` is the data — list of lists, each inner list being one row's
    pre-formatted string values. Rows must have the same length as
    `columns`; mismatch raises ValueError.

    Caller is responsible for value formatting (use formatting.py's
    fmt_dollars / fmt_shares / etc.). This writer just glues cells
    together with tabs and pipes.
    """
    n_cols = len(columns)

    # Header row. Leading "| " and trailing " |" with tab-pipe in
    # between, mirroring the sample format the operator confirmed.
    header_cells = [f"{c}\t" for c in columns[:-1]] + [f"{columns[-1]} "]
    file.write("| " + "| ".join(header_cells) + "|\n")

    # Separator row. `---` for each column, same tab+pipe rhythm.
    sep_cells = ["---\t"] * (n_cols - 1) + ["--- "]
    file.write("|" + "|".join(sep_cells) + "|\n")

    # Data rows
    for row in rows:
        if len(row) != n_cols:
            raise ValueError(
                f"Row has {len(row)} cells but table has {n_cols} columns: "
                f"{row}"
            )
        # Same tab+pipe rhythm as header. Empty cells render as
        # consecutive tab+pipe with no value between them.
        cells = [f"{v}\t" for v in row[:-1]] + [f"{row[-1]} "]
        file.write("| " + "| ".join(cells) + "|\n")
