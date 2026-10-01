#!/bin/bash
# Sequential timing pass. Do not run anything heavy while this runs.
set -x
cd "$(dirname "$0")/.."
python code/timing.py micro
python code/timing.py single --configs tfast_psm3,tfast_pro3,tbest_pro3_dfe,tbest_psm3,mupdf_statusquo,mupdf_300_full,tfast_psm4,tapi_fast_psm3,tfast_pro3_deskew,tbest_pro3,tfast_pro3_dfe,tfast_psm6,tfast_psm11,tbest_psm4,tfast_psm4_noinvert,tfast_psm4_sauvola,tfast_psm4_deskew,tfast_psm4_dfe,tbest_psm4_dfe,tfast_psm4_osd,tfast_psm4_autorot,tfast_psm3_nolines,tapi_hybrid85,tfast_psm4_400,tbest_psm4_400,rapidocr_bundled
python code/timing.py single --lowres --configs tfast_psm4,tfast_psm4_native,tfast_psm4_400,tfast_psm3,tfast_psm3_native,tfast_pro3,tbest_psm3,tbest_pro3_dfe
python code/timing.py through --configs tfast_psm3,tfast_pro3,tbest_pro3_dfe,tapi_fast_psm3,mupdf_300_full
echo TIMING_DONE
