# AutoCata Packaging

AutoCata exposes stable command-line entry points while keeping model and
training implementation inside the `autocata_core` package:

```text
public CLI scripts -> autocata_core -> Transformers/PyTorch
```

The package can be built as a wheel for internal distribution:

```bash
python -m pip install --upgrade build
python -m build --wheel
```

The resulting wheel contains the Python implementation. A wheel is a useful
installation boundary, but it is not source-code protection: Python bytecode
can be inspected by a determined user.

For a distribution where ordinary users should not receive readable Python
source, use a separate protected build pipeline on a private release machine:

1. keep `autocata_core/` in a private source repository;
2. build a platform-specific executable or compiled extension with Nuitka or
   an equivalent tool;
3. distribute only the CLI executable, public configs, environment file, and
   model-asset downloader;
4. keep checkpoints and MLP weights on the configured model-asset host.

This still cannot provide absolute secrecy. A local executable and its model
weights can be reverse-engineered, and the software license should state the
permitted use. The repository's normal development workflow remains source
based so that it can be tested and maintained.

The previous internal package name is intentionally not part of the public
import surface. Existing model checkpoint directory names are preserved
because they are model-asset identifiers, not Python package names.
