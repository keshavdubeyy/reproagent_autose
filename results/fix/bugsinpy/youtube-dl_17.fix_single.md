# ReproAgent fix report: `youtube-dl_17`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_single`, 1 patch attempt(s), 1 SLM calls, 39.8s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `youtube_dl/utils.py:cli_bool_option`

Regression tests run: test/test_utils.py, test/test_execution.py

### Proposed patch
```diff
--- a/youtube_dl/utils.py
+++ b/youtube_dl/utils.py
@@ -2733,6 +2733,8 @@
 
 def cli_bool_option(params, command_option, param, true_value='true', false_value='false', separator=None):
     param = params.get(param)
+    if param is None:
+        return []
     assert isinstance(param, bool)
     if separator:
         return [command_option + separator + (true_value if param else false_value)]
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **plausible**
