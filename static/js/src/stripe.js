(function () {
  const modal = document.getElementById("renewal-modal");

  const form = document.getElementById("payment-form");
  const paymentMethodDetails = document.getElementById(
    "payment-method-details"
  );
  const renewalCTAs = document.querySelectorAll(".js-renewal-cta");

  const addPaymentMethodButton = modal.querySelector(".js-payment-method");
  const cardExpiryEl = modal.querySelector(".js-customer-card-expiry");
  const cardImgEl = modal.querySelector(".js-customer-card-brand");
  const cardTextEl = modal.querySelector(".js-customer-card");
  const customerEmailEl = modal.querySelector(".js-customer-email");
  const customerNameEl = modal.querySelector(".js-customer-name");
  const loadingIndicator = modal.querySelector(".p-icon--spinner");
  const nameElement = modal.querySelector(".js-renewal-name");
  const processPaymentButton = modal.querySelector(".js-process-payment");
  const quantityElement = modal.querySelector(".js-renewal-quantity");
  const startElement = modal.querySelector(".js-renewal-start");
  const totalElement = modal.querySelector(".js-renewal-total");

  const resetModalButtons = modal.querySelectorAll(".js-reset-modal");

  const stripe = Stripe("pk_test_yndN9H0GcJffPe0W58Nm64cM00riYG4N46");
  const elements = stripe.elements();

  const style = {
    base: {
      iconColor: "#e95420",
      color: "#111",
      fontWeight: 300,
      fontFamily: "Ubuntu, sans-serif",
      fontSmoothing: "antialiased",
      fontSize: "18px",
      "::placeholder": {
        color: "#666",
      },
      ":-webkit-autofill": {
        color: "#666",
      },
    },
  };

  const card = elements.create("card", { style });
  const cardErrorElement = document.getElementById("card-errors");

  let accountID;
  let billingInfo;
  let cardInfo;
  let invoice;
  let paymentIntentStatus;
  let renewalID;
  let subscriptionStatus;

  attachCTAevents();
  attachModalButtonEvents();
  attachModalEvents();
  setupCardElements();

  function attachCTAevents() {
    renewalCTAs.forEach((cta) => {
      cta.addEventListener("click", () => {
        let renewalData = cta.dataset;

        modal.classList.remove("u-hide");
        accountID = renewalData.accountId;
        renewalID = renewalData.renewalId;

        setRenewalInformation(renewalData);
      });
    });
  }

  function attachModalButtonEvents() {
    addPaymentMethodButton.addEventListener("click", (e) => {
      e.preventDefault();
      createPaymentMethod();
    });

    processPaymentButton.addEventListener("click", (e) => {
      e.preventDefault();
      processStripePayment();
    });

    resetModalButtons.forEach((button) => {
      button.addEventListener("click", (e) => {
        e.preventDefault();
        resetModal();
      });
    });
  }

  function attachModalEvents() {
    function toggleModal(modal) {
      if (modal && modal.classList.contains("p-modal")) {
        modal.classList.toggle("u-hide");
      }
    }

    document.addEventListener("click", function (e) {
      let targetControls = e.target.getAttribute("aria-controls");

      if (targetControls) {
        toggleModal(document.getElementById(targetControls));
      }
    });
  }

  function createPaymentMethod() {
    let formData = new FormData(form);

    addPaymentMethodButton.disabled = true;

    stripe
      .createPaymentMethod({
        type: "card",
        card: card,
        billing_details: {
          name: formData.get("name"),
          email: formData.get("email"),
          address: {
            city: formData.get("city"),
            country: formData.get("country"),
            line1: formData.get("address"),
            postal_code: formData.get("postal_code"),
          },
        },
      })
      .then(function (result) {
        if (result.paymentMethod) {
          fetch("/advantage/payment-method", {
            method: "POST",
            credentials: "include",
            headers: {
              Accept: "application/json",
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              payment_method_id: result.paymentMethod.id,
              account_id: accountID,
            }),
          })
            .then((response) => {
              return response.json();
            })
            .then((data) => {
              if (data.message) {
                // this is not good, ua-contracts team are working on a better
                // way to return this data, but it's low on their priority list
                handleCardError(data.message);
              } else if (data.createdAt) {
                setPaymentInformation(result.paymentMethod);
                showPayDialog();
              } else {
                console.log(data);
                // TODO: how do we want to handle errors creating payment methods?
                // Feels like it wouldn't inspire confidence to ask the user to try
                // inputting them again.
                presentCardError();
              }
            });
        } else {
          presentCardError(result.error.message);
        }
      })
      .catch((error) => {
        // TODO handle this error
        console.log(error);
      });
  }

  function handleCardError(message) {
    const errorString = "unexpected error setting customer payment method: ";

    if (message.includes(errorString)) {
      const json_string = message.replace(errorString, "");
      const error_object = JSON.parse(json_string);

      presentCardError(error_object.message);
    } else {
      presentCardError();
    }
  }

  function handleIncompletePayment(invoice) {
    if (
      invoice.pi_status === "requires_payment_method" &&
      invoice.decline_code
    ) {
      console.log("reached if: ", invoice.decline_code);
    } else if (invoice.pi_status === "requires_action" && invoice.pi_secret) {
      stripe.confirmCardPayment(invoice.pi_secret).then(function (result) {
        if (result.error) {
          console.log("3D secure error: ", result.error);
        } else {
          console.log("3D secure success: ", result);
        }
      });
    } else {
      console.log("reached else: ", invoice);
    }
  }

  function handleIncompleteRenewal(renewal) {
    if (renewal.stripeInvoices) {
      invoice = renewal.stripeInvoices[renewal.stripeInvoices.length - 1];
      subscriptionStatus = invoice.subscription_status;

      paymentIntentStatus = invoice.pi_status;
    }

    if (!subscriptionStatus || !paymentIntentStatus) {
      setTimeout(() => {
        pollRenewalStatus();
      }, 3000);
    } else if (subscriptionStatus !== "active") {
      handleIncompletePayment(invoice);
    }
  }

  function handleSuccessfulPayment() {
    alert("payment successful");
    setTimeout(() => {
      location.reload();
    }, 3000);
  }

  function pollRenewalStatus() {
    fetch(`/advantage/renewals/${renewalID}`)
      .then((response) => {
        return response.json();
      })
      .then((renewal) => {
        if (renewal.status !== "done") {
          handleIncompleteRenewal(renewal);
        } else {
          handleSuccessfulPayment();
        }
      })
      .catch((error) => {
        // TODO handle this error
        console.log(error);
      });
  }

  function presentCardError(message = null) {
    if (!message) {
      message =
        "We encountered a problem while creating your payment method. Please contact support.";
    }
    cardErrorElement.textContent = message;
    cardErrorElement.classList.remove("u-hide");
  }

  function processStripePayment() {
    processPaymentButton.disabled = true;

    fetch(`/advantage/renewals/${renewalID}/process-payment`, {
      method: "POST",
      credentials: "include",
    })
      .then(() => {
        pollRenewalStatus();
      })
      .catch((error) => {
        // TODO handle this error
        console.log(error);
      });
  }

  function setRenewalInformation(renewalData) {
    let startDate = new Date(renewalData.start);

    let formattedTotal = parseFloat(renewalData.total).toLocaleString("en", {
      style: "currency",
      currency: renewalData.currency,
    });

    nameElement.innerHTML = `Renew "${renewalData.name}"`;
    quantityElement.innerHTML = `Quantity: ${renewalData.quantity}`;
    startElement.innerHTML = `Start date: ${startDate.toDateString()}`;
    totalElement.innerHTML = `Total: ${formattedTotal}`;
  }

  function setPaymentInformation(paymentMethod) {
    billingInfo = paymentMethod.billing_details;
    cardInfo = paymentMethod.card;

    const cardBrandFormatted =
      cardInfo.brand.charAt(0).toUpperCase() + cardInfo.brand.slice(1);
    const cardText = `${cardBrandFormatted} ending ${cardInfo.last4}`;
    const cardExpiry = `Expires: ${cardInfo.exp_month}/${cardInfo.exp_year}`;

    cardImgEl.innerHTML = cardInfo.brand;
    // TODO use the above to set an image of the card brand, rather than text
    cardTextEl.innerHTML = cardText;
    cardExpiryEl.innerHTML = cardExpiry;
    customerNameEl.innerHTML = billingInfo.name;
    customerEmailEl.innerHTML = billingInfo.email;
  }

  function setupCardElements() {
    card.mount("#card-element");

    card.on("change", ({ error }) => {
      if (error) {
        cardErrorElement.textContent = error.message;
        cardErrorElement.classList.remove("u-hide");
      } else {
        cardErrorElement.classList.add("u-hide");
      }
      addPaymentMethodButton.disabled = false;
    });
  }

  function showPayDialog() {
    form.classList.add("u-hide");
    paymentMethodDetails.classList.remove("u-hide");
    addPaymentMethodButton.classList.add("u-hide");
    processPaymentButton.classList.remove("u-hide");
    processPaymentButton.disabled = false;
  }

  function resetModal() {
    form.reset();
    card.clear();
    form.classList.remove("u-hide");
    paymentMethodDetails.classList.add("u-hide");
    addPaymentMethodButton.classList.remove("u-hide");
    addPaymentMethodButton.disabled = true;
    processPaymentButton.classList.add("u-hide");
    processPaymentButton.disabled = true;
  }
})();
