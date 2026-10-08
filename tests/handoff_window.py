"""Helper for test_handoff: plays "the BlamixShell that is already open" in its own process (so no Qt socket ever
lives in the pytest process). Prints READY, then GOT <request> for each request, and exits after the first one or
after 20 seconds."""
import json
import sys
import time

from PySide6.QtCore import QCoreApplication

from blamixshell import handoff

app = QCoreApplication([])
got = []
listener = handoff.listen(got.append)
if listener is None:
    print("NO-LISTEN", flush=True)
    sys.exit(1)
second = handoff.listen(lambda r: None)                  # a second launch must never take the name away
print("READY" if second is None else "SECOND-LISTENED", flush=True)
end = time.time() + 20
while not got and time.time() < end:
    app.processEvents()
    time.sleep(0.01)
for req in got:
    print("GOT", json.dumps(req, sort_keys=True), flush=True)
listener.close()
app.processEvents()
