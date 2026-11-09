// Relais d'e-mails Cotescope : à coller dans un projet Google Apps Script (script.google.com)
// sur le compte Gmail qui enverra les e-mails. Gratuit, environ 100 e-mails par jour.
// 1) Remplace CHANGE_MOI par un long mot de passe inventé (le même que le secret GitHub MAIL_RELAY_KEY).
// 2) Déployer > Nouveau déploiement > type « Application Web » ; Exécuter en tant que : Moi ;
//    Qui a accès : Tout le monde. Copie l'URL obtenue dans le secret GitHub MAIL_RELAY_URL.

var KEY = "CHANGE_MOI";

function doPost(e) {
  var out = { ok: false };
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.key === KEY && d.to && d.subject && d.html && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(d.to)) {
      MailApp.sendEmail({ to: d.to, subject: String(d.subject).slice(0, 200), htmlBody: String(d.html), name: "Cotescope" });
      out.ok = true;
    }
  } catch (err) {
    out.error = String(err).slice(0, 200);
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

// Pour autoriser l'envoi la première fois : sélectionne « autoriser » ci-dessus puis clique sur Exécuter.
function autoriser() {
  MailApp.getRemainingDailyQuota();
}
