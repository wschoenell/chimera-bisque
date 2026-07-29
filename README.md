# Bisque

Chimera plugin for Software Bisque TheSky/TheSkyX telescopes

This is a plugin for the [Chimera observatory control system](https://github.com/astroufsc/chimera).

## Installation

```bash
pip install -U chimera_bisque
```

Or install from source:

```bash
pip install -U git+https://github.com/astroufsc/chimera-bisque.git
```

## Configuration Example

Add the following to your `chimera.config` file:


```yaml
instruments:
    # TheSkyX over TCP/IP (recommended; works from any OS running chimera)
    - name: telescope
      type: TheSkyXTelescope
      skyx_host: localhost
      skyx_port: 3040
      find_home_on_unpark: True   # run FindHome when the mount is unparked
      max_find_home_time_sec: 300

    # TheSky 5/6 via Windows COM (requires Windows + the `windows` extra)
    - name: telescope
      type: TheSkyTelescope
      thesky: 6
```

The plugin ships two connection modes:

- **`TheSkyXTelescope`** talks to TheSkyX over its TCP/IP JavaScript scripting
  interface. It is pure Python and works on any platform. `unpark()` homes the
  mount (TheSkyX `FindHome`) and waits for it to arrive before returning; set
  `find_home_on_unpark: False` for mounts whose driver has no homing routine.
  Homing can also be triggered on its own with `find_home()`.
- **`TheSkyTelescope`** drives TheSky 5/6 through Windows COM automation. It
  only works on Windows with the optional `windows` extra installed
  (`uv sync --extra windows`, which pulls in `pywin32`). The plugin still
  installs and imports on non-Windows machines; the COM driver simply cannot
  connect there.

The obsolete `CCDSoftCamera` driver was removed; it is still available on the
`legacy` branch and in the git history if ever needed.




## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/astroufsc/chimera-bisque.git
cd chimera-bisque

# Install dependencies
uv sync

# Install pre-commit hooks
uv run pre-commit install --install-hooks
```

### Running Tests

```bash
uv run pytest
```

### Code Quality

This project uses:
- [Ruff](https://docs.astral.sh/ruff/) for linting and formatting
- [pre-commit](https://pre-commit.com/) for automated checks

```bash
# Run linter
uv run ruff check

# Run formatter
uv run ruff format

# Run all pre-commit hooks
uv run pre-commit run --all-files
```

## License

GPL-2.0-or-later

## Contact

For more information, contact us on chimera's discussion list:
https://groups.google.com/forum/#!forum/chimera-discuss

Bug reports and patches are welcome and can be sent over our GitHub page:
https://github.com/astroufsc/chimera-bisque
