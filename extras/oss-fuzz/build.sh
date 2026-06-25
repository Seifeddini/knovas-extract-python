#!/bin/bash -eu
# OSS-Fuzz build script for knovas-extract.
# Copy to projects/knovas-extract/build.sh in a google/oss-fuzz fork.
#
# Installs the project with all format extras, then registers every
# tests/fuzz/fuzz_*.py target with the OSS-Fuzz harness.

cd "$SRC/knovas-extract-python"

# Install the project + all format backends so the extractors are
# importable (and therefore actually exercised by the fuzzer).
pip3 install -e ".[all]"

# Build each atheris target via OSS-Fuzz's compile_python_fuzzer.
# Each target is a self-contained script in tests/fuzz/ that calls
# atheris.Setup + atheris.Fuzz on a single extractor's `extract()` path.
for fuzz_target in tests/fuzz/fuzz_*.py; do
  target_name=$(basename "$fuzz_target" .py)
  compile_python_fuzzer "$fuzz_target" \
    --hidden-import=knovas_extract \
    --hidden-import=knovas_extract.extractors.pdf \
    --hidden-import=knovas_extract.extractors.docx \
    --hidden-import=knovas_extract.extractors.html \
    --hidden-import=knovas_extract.extractors.rtf \
    --hidden-import=knovas_extract.extractors.eml \
    --hidden-import=knovas_extract.extractors.msg \
    --hidden-import=knovas_extract.extractors.txt \
    --hidden-import=knovas_extract.extractors.md
done

# Seed each target's corpus from the spec's golden fixtures.
# This gives OSS-Fuzz a real starting point instead of all-zero inputs;
# coverage ramps up faster.
if git clone --depth=1 https://github.com/Seifeddini/knovas-extract-spec.git "$SRC/spec"; then
  for target_name in fuzz_pdf fuzz_docx fuzz_html fuzz_rtf fuzz_eml fuzz_txt fuzz_md; do
    fmt="${target_name#fuzz_}"
    if [ -d "$SRC/spec/corpus/$fmt" ]; then
      mkdir -p "$OUT/${target_name}_seed_corpus"
      cp -L "$SRC/spec/corpus/$fmt/"*."$fmt" \
            "$SRC/spec/corpus/$fmt/"*."${fmt%/}" \
            "$OUT/${target_name}_seed_corpus/" 2>/dev/null || true
      (cd "$OUT" && zip -r "${target_name}_seed_corpus.zip" "${target_name}_seed_corpus/" >/dev/null && \
        rm -rf "${target_name}_seed_corpus/")
    fi
  done
fi
