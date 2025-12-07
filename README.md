# Traffic Codex

Utility script for Axis Q1806-LE cameras to swap image configuration between day
and night using the VAPIX `param.cgi` API. All cameras, credentials, and
profiles are defined in YAML so deployments can be driven by configuration.

## Setup

1. Install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Edit `axis_config.yaml` to match your environment:

   - Update the `cameras` section with hostnames/IPs. Credentials can be stored
     inline or via `username_env`/`password_env`.
   - Adjust `profiles` to the parameter sets you want to push. The included
     entries are sensible starting points for a Q1806-LE but the camera will
     only accept parameters it supports.
   - (Optional) Configure `schedules` that map times of day to profile names.

## Usage

   - Declare the username and password using

```bash
export AXIS_Q1806_USER="your_username"
export AXIS_Q1806_PASS="your_password"
```

Apply a profile to your camera by name:

```bash
python scripts/axis_image_switcher.py \
  --camera q1806_entry \
  --profile day
```

Additional options:

- `--config` — path to the YAML config (defaults to `axis_config.yaml`).
- `--timeout` — request timeout in seconds (defaults to 5).
- `--retries` — number of retries for transient network failures (defaults to 3).

Use a schedule to auto-select the profile based on the current time (e.g., for
cron):

```bash
python scripts/axis_image_switcher.py \
  --camera q1806_entry \
  --schedule day_night
```

The script exits with a clear error message if the profile cannot be found, the
camera rejects the parameters, authentication fails, or the network request
fails. It also makes a best effort to skip updates when the selected profile is
already applied on the camera.

## Notes

- Axis devices typically use HTTP digest authentication, which the script uses
  by default.
- If you maintain many cameras, keep separate config files per site and invoke
  the script from cron with a schedule name that maps to your desired day/night
  breakpoints.
