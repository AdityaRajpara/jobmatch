"""jobmatch: score job postings against a candidate profile with a language model.

The interesting parts are not the scoring. They are:
  - db.claim_job      one unit of work goes to exactly one worker
  - client.score      model output validated at the boundary, repaired or failed
  - worker._process   what happens when a worker dies halfway through
"""

__version__ = "0.1.0"
