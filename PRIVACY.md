# Privacy

contact-sync is software you run yourself, on your own server. There is no hosted service.

- **What it reads:** the contacts in the Microsoft 365 account and the Google account you sign in to.
- **Where the data goes:** only between those two accounts and your own server. Backups, reports
  and sign-in tokens are stored in the container's data folder on your server and nowhere else.
- **Address lookups (optional):** when the cleanup finds an address with a missing ZIP, city or state,
  it can look it up. It sends only the street line (house number and street, no suite), city, state
  and sometimes ZIP, never a name, phone or email: first to the US Census Bureau geocoder
  (geocoding.geo.census.gov), and if that has no match, to OpenStreetMap's address search
  (nominatim.openstreetmap.org, at most one request a second). Answers are cached on your server.
  Turn them off with the `address_lookup` / `osm_lookup` settings.
- **Who sees it:** nobody but you. The project's authors receive nothing: no analytics, no
  telemetry, no crash reports.
- **Google user data:** used only to back up, clean up and sync your own contacts. It is not sold,
  not used for advertising, and not shared with anyone.
- **Removing access:** delete the container's data folder, and remove the app's access in your
  Google account (Security > Third-party connections) and in Microsoft (My Apps or Entra admin center).

Questions: open an issue on the GitHub repository.
