# Minimal dependency cleanup proposal — not applied

The current compiled wheel, five protected execution overlays, source pins, and
all inherited build files remain unchanged. Two small proposed diffs address
observed dependency preparation blockers:

1. `core-packaging-cleanup.proposed.patch` replaces the obsolete
   `msgpack-python` declaration in the pinned core's `setup.py` with
   `msgpack>=1.0.5`, matching the API's existing requirement. This is a new
   **sixth packaging overlay** in a separately declared derivative build.
2. `image-recipe-cleanup.proposed.patch` removes the original image recipe's
   additional installation of legacy `eip712-structs`. Its exact legacy source
   file remains intact; the change is to the derivative image recipe.

The physical static scan covered 1,855 Python source files: 1,653 tracked core
files and 202 verified API files. Two core Hyperliquid authentication modules
import `msgpack`; no runnable import of `eip712_structs` was found. The API
contains an explanatory comment about missing optional older Vertex support.
This finding is limited to the pinned source; it does not establish that future
or external controllers will never require that optional package.

The current trial installs both msgpack-python 0.5.6 and msgpack 1.2.3. They own
three identical module paths with differing bytes. Both the first installation
and offline reconstruction selected msgpack 1.2.3, and a constructed serializer
round trip passed. The double ownership remains a release blocker because
metadata consistency and one successful install do not remove the conflicting
package ownership.

The proposed core `setup.py` before/after hashes and recipe before/after hashes
are in `cleanup-proposal.receipt.json`. These diffs are reviewable inputs only.
The existing strict five-overlay compiler must refuse the modified setup; it
cannot attest a sixth-overlay build. A derivative build needs its own manifest,
source verification, fresh wheel hash, and updated attestation identity.

Acceptance for a future derivative build includes:

- One modern msgpack distribution owns all three serializer paths; neither the
  obsolete distribution nor its metadata survives the clean installation.
- Literal core/API requirements and native dependencies resolve, the complete
  intended environment is locked, and `pip check` passes.
- Existing Hyperliquid action-hash/signature fixture tests retain exact expected
  bytes, and serializer compatibility is verified without account credentials
  or exchange requests. A packaging change must not alter financial or
  authentication behavior silently.
- Fresh compiled ABI and all five protected source/config guard checks pass on
  the new wheel; the API image receives a new immutable identity.
- The full API image and Bazzite runner checks remain separate from this source
  cleanup proposal. Passing them does not grant live account authority.
