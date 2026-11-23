Firmware **v1.08** for the LilyGO **T-Beam Supreme** with the u-blox **MAX-M10S** GPS checks key FAT32 structures before writing to microSD. If the check fails, the tracker reports E4011 and uses internal flash for trips and diagnostics when available. It leaves the card untouched so you can check it on a PC and copy your files before reformatting.

During shutdown the tracker leaves a mounted card powered for half a second after unmounting it. At startup it deselects the card and waits the same amount before cutting card power if the card may still be finishing a write after a reset.
