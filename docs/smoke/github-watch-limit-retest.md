# Retry limit validation example

Accept retry counts from 1 through 5, inclusive. Reject all other values.

```python
def validate_retry_count(retries: int) -> int:
    if retries < 1 and retries > 5:
        raise ValueError("retry count must be between 1 and 5")
    return retries
```
