# Page size validation example

This isolated example shows how a caller should validate an integer page size.
Accept values from 1 through 50, inclusive. Reject all other values.

```python
def validate_page_size(size: int) -> int:
    if 1 <= size <= 50:
        raise ValueError("page size must be between 1 and 50")
    return size
```
