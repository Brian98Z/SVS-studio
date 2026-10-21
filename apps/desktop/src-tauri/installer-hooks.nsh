; Older installers accidentally included executable packaging license helpers.
; Remove only those obsolete application-owned paths during an in-place upgrade.
!macro NSIS_HOOK_PREINSTALL
  Delete "$INSTDIR\runtime\licenses\python\packaging\packaging\licenses\__init__.py"
  Delete "$INSTDIR\runtime\licenses\python\packaging\packaging\licenses\_spdx.py"
  Delete "$INSTDIR\runtime\licenses\python\pip\pip\_vendor\packaging\licenses\__init__.py"
  Delete "$INSTDIR\runtime\licenses\python\pip\pip\_vendor\packaging\licenses\_spdx.py"
  Delete "$INSTDIR\runtime\licenses\python\setuptools\setuptools\_vendor\packaging\licenses\__init__.py"
  Delete "$INSTDIR\runtime\licenses\python\setuptools\setuptools\_vendor\packaging\licenses\_spdx.py"
!macroend
