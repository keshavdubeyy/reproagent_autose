Title: `bool(tqdm(generator))` raises / is wrong for iterables without a length

Wrapping a generator, e.g. `t = tqdm(x for x in [1, 2, 3])`, and then doing `if t:` or `bool(t)` fails with a
TypeError instead of returning True. A tqdm wrapping an iterable should be truthy even when the iterable has no
`len()`. (Behaviour for `tqdm(total=0)` -> False and `tqdm(total=10)` -> True should stay as is.)
