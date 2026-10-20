# Reproducible native inference components (SF-073)

SuiFleet supplies executable `suifleet-ml-classifier` and `suifleet-ml-detector`
1.0.0 components. Each is a deterministic ZIP application containing its model,
runner, image fixtures, notices, and exact runtime wheels. Install extracts a
private, workspace-local runtime without pip, a package manager, or network
access. Classification and detection execute the actual TensorFlow Lite CPU
interpreter, with one thread and default delegates disabled. These are small
analytical examples, not pretrained natural-image classifiers or general object
detectors. They demonstrate artifact delivery and real inference, not model
accuracy on external datasets.

The [runtime lock](../interfaces/compat/ml/runtime-lock.json) pins TensorFlow Lite
2.14.0 and NumPy 1.26.4 wheel URLs, SHA-256 hashes and license notices. The
[model contract](../interfaces/compat/ml/models.json) pins original model and
fixture digests, preprocessing, labels, boxes and absolute output tolerance
`1e-6`. The generator uses analytical weights and no external training data.
Original models, images and code use the repository MIT license.

| Bundle selection | Required ABI | Evidence |
|---|---|---|
| `linux-x86_64-cp310` | CPython 3.10, glibc >=2.17 | Actual host/container inference |
| `linux-aarch64-cp310` | CPython 3.10, glibc >=2.34 | Hash/license checked wheels and deterministic packaging; execution unverified |

The runtime checks the actual OS, architecture, Python ABI and glibc before
installation or imports. Other targets, including 32-bit ARM, Windows and macOS,
are rejected. A Pi's CPU alone does not establish this userspace ABI. No Pi
image, physical execution, camera integration or performance result is claimed.

## Reviewed upstream semantic references

Reviewed 2026-10-16, before implementing the runner. The AWS pages define the
functional separation between model artifacts, runtime installation and inference.
They are specification references, not execution evidence or source code used in
this implementation. Their component versions differ from the native runtime:

| AWS semantic reference | Exact component version |
|---|---|
| [TensorFlow Lite runtime](https://docs.aws.amazon.com/greengrass/v2/developerguide/tensorflow-lite-component.html) | 2.5.15 (installs TF Lite 2.5.0) |
| [DLR runtime](https://docs.aws.amazon.com/greengrass/v2/developerguide/dlr-component.html) | 1.6.16 |
| [TF Lite classification](https://docs.aws.amazon.com/greengrass/v2/developerguide/tensorflow-lite-image-classification-component.html) | 2.1.12 |
| [TF Lite detection](https://docs.aws.amazon.com/greengrass/v2/developerguide/tensorflow-lite-object-detection-component.html) | 2.1.12 |
| [DLR classification](https://docs.aws.amazon.com/greengrass/v2/developerguide/dlr-image-classification-component.html) | 2.1.14 |
| [DLR detection](https://docs.aws.amazon.com/greengrass/v2/developerguide/dlr-object-detection-component.html) | 2.1.14 |

The native implementation uses the upstream
[v2.14.0 Interpreter](https://github.com/tensorflow/tensorflow/blob/v2.14.0/tensorflow/lite/python/interpreter.py)
and [schema-v3 format](https://github.com/tensorflow/tensorflow/blob/v2.14.0/tensorflow/lite/schema/schema.fbs).
No AWS model or inference implementation is copied. There is no DLR execution,
SageMaker service, AWS recipe, AWS IPC, generic-wrapper or whole-catalog
compatibility claim. Existing AWS catalog dispositions and SF-064 reference
vectors remain specification-only; these native components do not satisfy an
AWS-named recipe dependency automatically.

## Build and run

From the checkout on the selected build host (cross-packaging is allowed):

```bash
python3 scripts/ml_component.py download --platform linux-x86_64-cp310 --wheels .ci-cache/ml-wheels
python3 scripts/ml_component.py build --task classifier --platform linux-x86_64-cp310 --wheels .ci-cache/ml-wheels --out /tmp/classifier.pyz
python3 scripts/ml_component.py build --task detector --platform linux-x86_64-cp310 --wheels .ci-cache/ml-wheels --out /tmp/detector.pyz
python3 -I -B /tmp/classifier.pyz install --state /tmp/classifier-runtime
python3 -I -B /tmp/classifier.pyz infer --state /tmp/classifier-runtime --fixture bright
python3 -I -B /tmp/detector.pyz install --state /tmp/detector-runtime
python3 -I -B /tmp/detector.pyz infer --state /tmp/detector-runtime --fixture marker
# An operator-provided input instead of a bundled fixture:
python3 -I -B /tmp/detector.pyz infer --state /tmp/detector-runtime --image interfaces/compat/ml/images/two-markers.pgm
```

Only `download` accesses the network, and it verifies each wheel before saving.
Existing corrupt cache entries fail; they are not silently replaced. Builds
verify model, image, notice and wheel hashes. Fixed ZIP entry order, timestamps,
permissions and stored compression produce identical bytes for identical inputs.
Both tasks deliberately include the same pinned runtime payload so they can be
installed independently without cross-component filesystem access. A build is
about 21 MB on x86_64; reserve space for the bundle, extracted runtime and staging.

To reproduce the tiny model/image fixtures, install generation-only dependencies
in a disposable directory and compare against the committed hashes:

```bash
python3 -m pip install --target /tmp/ml-generator tflite==2.18.0 flatbuffers==25.2.10 numpy==1.26.4
PYTHONPATH=/tmp/ml-generator python3 scripts/generate_ml_models.py
git diff --exit-code -- interfaces/compat/ml/models interfaces/compat/ml/images
```

Generation packages never ship in the component and are not device dependencies.
Model changes require a reviewed version/hash/expected-output change together.

## Input and output contract

Input is exactly `P5\n4 4\n255\n` followed by 16 unsigned grayscale bytes.
Reject all other headers, dimensions, truncation and extra bytes. There is no
implicit image decoding, resize, crop or RGB conversion. Preprocessing converts
to float32 and divides by 255. The classifier uses row-major `[1,16]` input and
outputs two brightness scores `[1-mean, mean]`. Highest score chooses `dark` or
`bright`; ties choose `dark`. These are not calibrated confidence probabilities.

The detector uses NHWC `[1,4,4,1]` input and a 2x2 convolution with stride two.
Its four output scores are mean brightness for non-overlapping tiles in row-major
order. Scores >=0.75 yield `bright-marker` detections with exact integer pixel
`[xmin,ymin,xmax,ymax]` boxes. Tiles are independent; there is no NMS or merging.
The marker fixture produces scores `[1,0,0,0]` and box `[0,0,2,2]`. The classifier
returns `[0.75,0.25]` and `dark` for that same image. Gray and tie fixtures exercise
normalization and decision boundaries. Reports include model/runtime versions,
model/input digests, raw scores and task-specific labels/boxes.

## Authentication, installation and recovery

After uploading each built artifact through the existing authorized operator
workflow, generate its native recipe with the returned blob ID:

```bash
python3 scripts/ml_component.py recipe --artifact /tmp/classifier.pyz --blob-id RETURNED_BLOB_ID --out /tmp/classifier-recipe.json
```

Use the existing [catalog publication](COMPONENT_CATALOG.md) flow to bind the
recipe SHA-256 and artifact SHA-256 to the immutable component version. Publish
separate Linux x86_64 and aarch64 manifests using the respective bundles/recipes;
the daemon selects by OS/architecture and the runner additionally checks the ABI.
No wildcard architecture manifest is appropriate. This command creates local
recipe bytes only; it does not publish or spend gas.

The generated recipe uses schema 1.0 with an `install` lifecycle that installs
the runtime and executes the selected model on the marker fixture. Enable
`[apply] allow_recipe_commands = true` explicitly for trusted recipe commands.
The workspace `{artifact}` and `{artifacts}` placeholders use the daemon's
existing shell quoting. The supervised `run` lifecycle repeats marker inference every 60 seconds and
exits on failure; `readiness` requires a successful inference. This is an example
worker, not an inference server or firmware health check. The existing supervisor
retains the authenticated bundle as `artifact` and the installed `ml-runtime` in
`<state_dir>/supervised/suifleet-ml-<task>/versions/1.0.0-<deployment-tag>/`,
independently of the temporary deployment directory. Read `active.json` in
`<state_dir>/supervised/` for the selected deployment/version; do not select an
arbitrary retained version. Authorized local consumers invoke
`python3 -I -B <version-directory>/artifact infer --state <version-directory>/ml-runtime --image /path/to/input.pgm`
and check its exit/result. Normal supervisor replacement stops the old worker
and installs into a separate deployment workspace; failed replacement retains
the prior active version under the existing recovery policy. A clean daemon
restart resumes the committed worker. Old version retention/cleanup follows the
existing supervisor policy; no global Python environment is modified.

The chain-bound outer artifact digest is the trust root. The runner's internal
manifest alone cannot authenticate a bundle supplied with an attacker-chosen
expected digest. Before extracting or importing native libraries, the runner
checks inventory, model/runtime versions, target ABI and every payload digest.
Inference also hashes the entire installed runtime and rejects missing, modified,
extra or symlinked files before importing it, then checks installed package
versions and exact model input/output shapes and float32 types. It requires
Python isolated mode (`-I`) and disables bytecode writes (`-B`). These integrity
checks do not create a sandbox or remove the normal trusted-publisher/native-code
boundary; keep the component workspace private and protected from concurrent
writers under the existing component identity policy.

Install writes only to a private staging directory before a final rename.
An interrupted stage is never imported. A retry can install into an absent
destination and verify/reuse a fully installed identical runtime. A corrupt
destination is rejected and requires explicit operator repair or a new workspace;
it is never automatically deleted or overwritten. Abandoned `.ml-stage-*`
directories can be inspected and removed after stopping the installer. This is
software installation recovery, not firmware rollback or power-loss durability.

TF Lite's PyPI wheels omit license files, so both bundles additionally retain
the exact TensorFlow v2.14.0 Apache license and full upstream third-party notices.
NumPy's BSD license and bundled dependency notices remain inside its unmodified,
hash-checked wheel and extracted runtime. The lock records every included wheel
notice's digest; the model contract hashes the separately retained TF notices.
No proprietary AWS model artifacts or production credentials are included.

## Verification and limits

`make test-ml` runs in ordinary `make ci`/`make check`. It downloads only the
locked x86_64 wheels, then runs offline subprocess tests with actual native
libraries. The real-daemon gRPC/Walrus regression deploys both exact generated
recipes and proves inference after temporary deployment cleanup, same-component
replacement and a clean daemon restart. The tests check all five image fixtures for both tasks, exact labels
and boxes and per-element tolerance; deterministic bundles; generated recipe
execution; wrong model/digest/version/input; unsupported platform/libc;
wheel/license and installed-library tampering; symlink/path traversal rejection;
and real SIGKILL during extraction followed by failed premature inference and a
successful fresh install/inference. Failed input followed by a valid request
also verifies application recovery without fabricated success.

Physical target and aarch64 execution remain unrun. Physical Pi integration
stays in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32), and
SF-066's refreshed image remains on hold. No cloud API is used by these examples;
live AWS acceptance is unauthorized and unrun, recorded separately in
[SF-073/#127](https://github.com/tyler-suifleet/suifleet/issues/127). Normal exact-head
CI and automatic Sui testnet status still apply to the PR; neither a local
inference result nor a package-upgrade result proves AWS compatibility, Walrus
certification, physical performance, or production readiness.

The locked CPython 3.10 bundles do not match the newer Python ABI in the pinned
Yocto images. Image ABI/runtime selection and actual ARM execution remain target
acceptance in SF-031/#32; no working Yocto ML runtime is claimed.
