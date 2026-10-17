name: Pull request
description: Propose a change to the codebase.
title: ""
body:
  - type: textarea
    id: what
    attributes:
      label: What this changes
    validations:
      required: true
  - type: textarea
    id: why
    attributes:
      label: Why
      description: The problem this solves. If it fixes a bug, include the input that reproduced it.
    validations:
      required: true
  - type: textarea
    id: how_tested
    attributes:
      label: How you tested it
      description: Commands run, tests added, real sites exercised.
    validations:
      required: true
  - type: checkboxes
    id: invariants
    attributes:
      label: Invariants
      description: This project guarantees the following. Please confirm your change keeps them true.
      options:
        - label: A crash cannot lose a row that was already written
        - label: An "ok" row means a real page was fetched and parsed (no success status on an empty body or bot challenge)
        - label: "`robots.txt` handling stays honest: honoured by default, and disallowed URLs are reported, not silently dropped"
        - label: Memory stays flat — rows stream to the output rather than accumulating
        - label: List columns stay deduplicated and sorted, and count columns equal their list lengths
    validations:
      required: true
  - type: checkboxes
    id: checks
    attributes:
      label: Checks
      options:
        - label: "`python -m unittest discover` passes locally"
        - label: No new runtime dependency was added (or it is justified in the description)
        - label: README updated if user-visible behaviour or flags changed
    validations:
      required: true
