
ANDROID_VERSION = android_6.0.1_r1
TOOLS = $(shell pwd)/tools
PYTHON = python2.7

all: interface stub packageBuilder

pip:
	sudo pip install ply==3.6
	sudo pip install plyj==0.1
	sudo pip install simplejson==3.8.0
	
interface:
	echo Collecting Interface...........
	$(PYTHON) $(TOOLS)/CollectIInterface.py
	$(PYTHON) $(TOOLS)/dumpTransactionCodeFromInterface.py

stub:
	echo Translating stub...............
	$(PYTHON) $(TOOLS)/stubTranslator.py

packageBuilder:
	echo Building package...............
	$(PYTHON) $(TOOLS)/PackageBuilder.py


