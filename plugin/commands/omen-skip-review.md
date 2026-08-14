---
description: Stop Omen blocking flashes for the next hour (bench mode)
argument-hint: "[minutes] [reason: bench|wrong|urgent]"
---

Call the omen MCP tool `skip_review`.

- `minutes`: from `$ARGUMENTS` if a number is given, otherwise 60.
- `reason`: from `$ARGUMENTS` if one of `bench`, `wrong`, `urgent` is given,
  otherwise `bench`.

Pass the reason accurately — it is not cosmetic. `wrong` means the reviewer's
finding was bad and feeds our suppression rules; `bench` means the gate was
right and the engineer chose speed. Recording `bench` as `wrong` makes our
false-positive rate report failures the gate never had, so if the user did
not say the finding was wrong, do not claim they did.

Then tell the user, in one line, when the gate comes back and that
`/omen-enable-review` restores it early.
