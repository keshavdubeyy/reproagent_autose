Title: TypeError when using `unit_scale` with an iterator of unknown length

    for i in tqdm((i for i in range(10)), unit_scale=10):
        pass

crashes with `TypeError: unsupported operand type(s) for *=: 'NoneType' and 'int'`. With no known total it should
just show the scaled count (e.g. "100it") instead of crashing.
