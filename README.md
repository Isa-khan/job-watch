# job-watch

Early-career radar, every 15 minutes on GitHub Actions (free, public repo).

**Sources:** SimplifyJobs + vanshb03 new-grad/intern trackers, speedyapply, the Canada internships list,
Amazon's job search, ~1,800 Greenhouse/Ashby/Lever/SmartRecruiters/Workday boards polled directly
(auto-discovered from the trackers, so direct polling usually beats the trackers), and a few careers pages.

**Feed:** every new intern + new grad posting lands in [`state/feed/`](state/feed) with a UTC timestamp.

**Email (GitHub issue):** only new grad / entry-level roles that are in Canada or remote, or US-based at a
watchlist company. Nothing new → no email. Close issues once handled.
