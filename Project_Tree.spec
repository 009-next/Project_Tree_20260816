# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols',
        'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on',
        'projecttree.security', 'projecttree.exporters', 'projecttree.intake', 'projecttree.provider',
        'anthropic', 'llm', 'multipart', 'python_multipart', 'pptx', 'openpyxl', 'fitz',
        # Enhancement02 対応で追加したモジュール群と、その外部依存
        'projecttree.autorun', 'projecttree.blender', 'projecttree.docs', 'projecttree.docthread',
        'projecttree.foldersync', 'projecttree.illustrate', 'projecttree.inference',
        'projecttree.masking', 'projecttree.modelgen', 'projecttree.models', 'projecttree.progress',
        'projecttree.projects', 'projecttree.slides', 'projecttree.stages', 'projecttree.vision',
        # 記録から2D/3Dを起こす経路と、公開URL用の閲覧専用ガード
        'projecttree.modelgen_llm', 'projecttree.readonly', 'projecttree.visibility',
        'projecttree.shapes_ext', 'projecttree.autoingest', 'projecttree.fastextract',
        'projecttree.docthread', 'projecttree.foldersync',
        'docx', 'docx.shared', 'docx.enum.text',
        'trimesh', 'trimesh.exchange.gltf', 'trimesh.exchange.stl',
        'ezdxf', 'numpy', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'transformers', 'optree', 'pandas', 'scipy', 'matplotlib', 'gradio', 'sentry_sdk', 'opentelemetry', 'networkx', 'sympy', 'polars', 'PyQt5', 'tokenizers', 'onnxruntime', 'pyiceberg', 'sklearn', 'scikit-learn'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Project_Tree',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Project_Tree',
)
