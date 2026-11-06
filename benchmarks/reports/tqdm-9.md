Title: Wrong SI-prefixed numbers near unit boundaries, and `len()` of a tqdm over a range

1. `tqdm.format_meter(1, 9999, 1, unit_scale=True)` should display the total as `10.0K`, and
   `format_meter(1, 999999, 1, unit_scale=True)` as `1.00M`, but values just below a unit boundary are shown
   wrongly (e.g. `1000K` / `10.00K`-style output instead of rolling over to the next prefix).
2. `len(tqdm(range(10)))` should be 10.
