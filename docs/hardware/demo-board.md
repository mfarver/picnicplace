# PicnicPlace demo board (concept)

A board PicnicPlace plans to publish for owners to order and assemble as their first job.
It also measures how accurately the machine places. It is the partner of the
[datum](picnicplace-datum.md): the datum calibrates the machine, and the demo board shows
that the calibration worked. It is a concept, and every dimension is still rough.

## Why one board

- **A first job.** A new owner needs a board they can order cheaply, with parts that are
  easy to buy, to try every step once: import, Prepare, loading feeders, a run, and
  reflow.
- **An accuracy test.** "It places to within 0.1 mm" should come with evidence. After a
  run, the top camera can look at each part on the demo board and measure how far it
  landed from where the board wanted it.
- **One reference for everyone.** When every machine runs the same board, bug reports,
  flight recordings and accuracy figures can be compared, between machines and between
  versions of PicnicPlace.
- **The same board everywhere.** Today's tests, the simulator and the website use
  `picnic-demo`, a made-up board in `core/tests/data/kicad` that has never been made.
  It was drawn for what the importer must get right: both sides, every quarter turn and
  one odd angle, a through-hole part, a do-not-populate part, and a part with no part
  number. The real board would take its place in the simulator and the screenshots, so
  what the website shows is a board people can order. The test fixture stays for the
  importer's edge cases.

## The idea

- **Size:** about 80 × 60 mm, two layers, 1.6 mm thick, so it sits in the datum's corner
  and stays within the cheapest pricing at most fabs. A KiKit panel of two, with its own
  panel fiducials, tests panels.
- **Fiducials:** three 1 mm copper dots with 3 mm mask openings, placed asymmetrically, so
  a board loaded turned round is caught instead of placed wrong.
- **Parts that cover what a machine meets:**
  - passives in 0402, 0603, 0805 and 1206;
  - polarized parts: an SOD-123 diode and 0805 LEDs, where a wrong turn is visible;
  - SOT-23 and SOT-23-6;
  - an SOIC-8 on 12 mm tape, and a QFN that needs the bottom camera to align its pads;
  - every quarter turn, and one part at 45°.

  All are common and cheap, nearly all on 8 mm tape, so a few feeders hold the whole
  board.
- **Accuracy targets.** Some positions are targets rather than circuit: pads that get no
  paste, each footprint outlined by a thin copper ring the top camera can see. Parts
  placed there on a tacky film, instead of paste, can be measured against the ring and
  picked up again. The result is a report: each part's offset and turn from its target,
  and the spread for each nozzle.
- **It does something.** Powered from USB-C, the LEDs light, so a finished board shows
  that the parts went in the right way round. The connector is fitted by hand.
- **Open design files.** A KiCad 10 project in the repository, with a BOM of generic
  parts that several suppliers carry, the KiKit panel, and the job file ready to run.

## In PicnicPlace

All planned:

- **Load the demo board** in Prepare opens the published job, so a new owner needs no
  KiCad files of their own.
- **Inspect placements** runs the top camera over the placed parts and writes
  the accuracy report, kept with the run and sent with a flight recording.
- The simulator offers the same board, with its parts already in the library, on tapes,
  and assigned to simulated feeders, so the screenshots and the tutorials match a real
  run.

## Open questions

- **License.** Hardware designs are usually licensed under a hardware license such as
  CERN-OHL-P (permissive) or CERN-OHL-S (reciprocal, like the MPL), rather than the MPL
  the software uses.
- **Size, and whether to panel by default.** A smaller board is cheaper; a panel of two
  exercises more of PicnicPlace.
- **The circuit.** LEDs alone, or something a little more useful (a blinker, a USB
  continuity tester) without adding parts a new owner struggles to buy.
- **Parts kits.** Whether to list one supplier's part numbers for a ready-made kit, or
  only generic values.
- **Paste.** A stencil for the full board, and the accuracy targets run without paste.
