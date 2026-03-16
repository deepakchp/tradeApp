import os
from setuptools import setup, Extension
from Cython.Build import cythonize

# Set optional compile arguments for optimization
extra_compile_args = []
if os.name == 'nt':  # Windows
    extra_compile_args.extend(['/O2'])
else:  # Linux/MacOS
    extra_compile_args.extend(['-O3', '-ffast-math', '-march=native'])

ext_modules = [
    Extension(
        "modules.fast_greeks",
        ["modules/fast_greeks.pyx"],
        extra_compile_args=extra_compile_args,
    )
]

setup(
    name="fast_greeks",
    ext_modules=cythonize(ext_modules, compiler_directives={'language_level': "3", 'boundscheck': False, 'wraparound': False, 'cdivision': True}),
)
