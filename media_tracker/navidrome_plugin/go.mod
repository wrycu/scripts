module like-dislike

go 1.25

require github.com/navidrome/navidrome/plugins/pdk/go v0.0.0

// build.sh stages this plugin under a navidrome checkout at
// plugins/examples/like-dislike/, so the PDK resolves via this relative path
// exactly like the bundled example plugins.
replace github.com/navidrome/navidrome/plugins/pdk/go => ../../pdk/go
