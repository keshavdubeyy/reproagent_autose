Title: Escaping does not work for the long `:=` separator in request items

In httpie request items a backslash escapes a separator. `bob\:==foo` should be parsed as a data field with key
`bob:=` and value `foo` (the escaped `:=` is part of the key, the real separator is `=`). Instead the item is
split at the wrong place. The parsing happens in `httpie.cli.parse_items`.
