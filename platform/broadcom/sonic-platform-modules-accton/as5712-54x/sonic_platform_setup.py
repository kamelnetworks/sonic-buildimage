from setuptools import setup

setup(
    name='sonic-platform',
    version='1.0',
    description=f'SONiC PDDF platform API PDDF for x86_64-accton_as5712_54x-r0',
    license='Apache 2.0',
    author='SONiC Team',
    author_email='linuxnetdev@microsoft.com',
    url='https://github.com/sonic-net/sonic-buildimage',
    packages=[
        'sonic_platform',
    ],
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Plugins',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3.7',
        'Topic :: Utilities',
    ],
    keywords='sonic SONiC platform PLATFORM',
)
