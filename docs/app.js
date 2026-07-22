// language toggle
document.querySelectorAll('.lang-toggle button').forEach(function (b) {
  b.addEventListener('click', function () {
    var lang = b.dataset.lang;
    document.body.className = 'lang-' + lang;
    document.querySelectorAll('.lang-toggle button').forEach(function (x) {
      x.classList.toggle('active', x.dataset.lang === lang);
    });
  });
});

// agency filter
document.querySelectorAll('.chip').forEach(function (chip) {
  chip.addEventListener('click', function () {
    var ag = chip.dataset.agency;
    document.querySelectorAll('.chip').forEach(function (c) { c.classList.remove('active'); });
    chip.classList.add('active');
    document.querySelectorAll('.card').forEach(function (card) {
      var list = (card.dataset.agencies || '').split('|');
      var show = ag === '__all' || list.indexOf(ag) !== -1;
      card.classList.toggle('hidden', !show);
    });
    // hide a theme section if all its cards are filtered out
    document.querySelectorAll('[data-theme]').forEach(function (sec) {
      var visible = sec.querySelectorAll('.card:not(.hidden)').length;
      sec.classList.toggle('hidden', visible === 0);
    });
  });
});

// mobile sidebar
var menu = document.getElementById('menu-btn');
if (menu) menu.addEventListener('click', function () {
  document.getElementById('sidebar').classList.toggle('open');
});

// "In the Media" filters: tone, agency, and whether the cited source was identified
document.querySelectorAll('[data-mfilter]').forEach(function (chip) {
  chip.addEventListener('click', function () {
    var f = chip.dataset.mfilter;
    document.querySelectorAll('[data-mfilter]').forEach(function (c) { c.classList.remove('active'); });
    chip.classList.add('active');
    document.querySelectorAll('.mention').forEach(function (card) {
      var show = true;
      if (f.indexOf('tone:') === 0) {
        show = card.dataset.tone === f.slice(5);
      } else if (f.indexOf('agency:') === 0) {
        show = (card.dataset.agencies || '').split('|').indexOf(f.slice(7)) !== -1;
      } else if (f.indexOf('src:') === 0) {
        show = card.dataset.matched === f.slice(4);
      }
      card.classList.toggle('hidden', !show);
    });
  });
});
