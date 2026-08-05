

class TestMakeMkvKnowsWhatTheDiscIsCalled:
    """A rip came out as "Unknown - pt1.mp4" while MakeMKV had the disc's name
    the whole time.

    blkid reads the label, and blkid times out on a busy drive — which the
    drive is for the entire rip. MakeMKV reports the same name in its CINFO
    records, parsed into RipResult.disc_name since the first version of the
    ripper and never read by anything.
    """

    def test_the_ripper_still_parses_it(self):
        from adr.ripper import MakeMKVRipper, RipResult

        result = RipResult()
        MakeMKVRipper._parse_cinfo('CINFO:0,2,0,"DINOSAUR"', result)
        assert result.disc_name == "DINOSAUR"

    def test_the_pipeline_falls_back_to_it(self):
        """Read rather than run: the fallback sits in the middle of a method
        that needs a disc, a database and a drive."""
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "rip_result.disc_name" in source, (
            "the disc name MakeMKV reports is unused again"
        )

    def test_a_label_beats_makemkvs_name(self):
        """blkid's label is the filesystem's own, and closer to the release
        name than MakeMKV's guess when both exist."""
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "volume_name or rip_result.disc_name" in source
