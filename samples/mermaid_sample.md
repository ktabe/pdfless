# Markdown and Mermaid sample

A short document for trying out `pdfless` on Markdown: headings, lists, a
table, a code block, and [Mermaid](https://mermaid.js.org) diagrams.

## Rendering pipeline

`pdfless` turns a Markdown file into pages and shows them in the terminal.

```mermaid
flowchart LR
    A[Markdown] --> B[HTML]
    B --> C[PDF]
    C --> D[Page images]
    D --> E[Terminal]
```

- Headings become the table of contents (`o`).
- Links stay clickable.
- Press `t` to see the source.

## Supported content

| Element | Rendered as |
| --- | --- |
| Headings | Bookmarks in the table of contents |
| Tables | Tables with borders |
| Code blocks | Monospaced text on a gray background |
| `mermaid` blocks | Diagrams |

<div style="page-break-before: always"></div>

## Opening a file

What happens between starting `pdfless` and seeing the first page:

```mermaid
sequenceDiagram
    participant U as User
    participant P as pdfless
    participant T as Terminal
    U->>P: pdfless notes.md
    P->>P: Render pages
    P->>T: Inline image
    T-->>U: First page
    U->>P: Press n
    P->>T: Next page
```

## Example code

```python
def greet(name: str) -> str:
    """Return a greeting for `name`."""
    return f"Hello, {name}!"
```
