# Batch size validation example

A batch must contain at least one item and at most 25 items.

```python
def validate_batch_size(size: int) -> int:
    if size < 0 or size > 25:
        raise ValueError("batch size must be between 1 and 25")
    return size
```
