# INVM SAI

INVM_SAI_ONLINE = https://github.com/Innovium/SONiC/raw/master/debian/master

INVM_LIBSAI = isai.deb
INVM_HSAI   = saihdr.deb
INVM_DRV    = ipd.deb
INVM_CONFLICT_DEB = innovium-sai-headers

$(INVM_LIBSAI)_URL = $(INVM_SAI_ONLINE)/$(INVM_LIBSAI)
$(INVM_HSAI)_URL   =  $(INVM_SAI_ONLINE)/$(INVM_HSAI)
$(INVM_DRV)_URL    =  $(INVM_SAI_ONLINE)/$(INVM_DRV)

$(eval $(call add_conflict_package,$(INVM_CONFLICT_DEB),$(LIBSAIVS_DEV)))

SONIC_ONLINE_DEBS  += $(INVM_LIBSAI) $(INVM_HSAI) $(INVM_DRV)
