/* =========================================
   ABOUT PAGE JAVASCRIPT
   DEPOT LES SOURCES
========================================= */


/* ===============================
   MOBILE MENU
================================ */

const menuToggle = document.querySelector(".menu-toggle");
const navLinks = document.querySelector(".nav-links");


if(menuToggle){

    menuToggle.addEventListener("click",()=>{

        navLinks.classList.toggle("active");

    });

}





/* ===============================
   ANIMATED STATISTICS COUNTER
================================ */


const counters = document.querySelectorAll(".stat-item h2");


const startCounter = (counter)=>{


    const target = Number(counter.dataset.number);


    if(!target) return;


    let count = 0;


    const speed = target / 80;



    const updateCounter = ()=>{


        if(count < target){

            count += Math.ceil(speed);

            counter.textContent = count + "+";

            setTimeout(updateCounter,30);

        }

        else{

            counter.textContent = target + "+";

        }


    };


    updateCounter();


};






/* Activate counters when visible */

const observer = new IntersectionObserver((entries)=>{


    entries.forEach(entry=>{


        if(entry.isIntersecting){


            startCounter(entry.target);


            observer.unobserve(entry.target);


        }


    });


},{


    threshold:0.5

});



counters.forEach(counter=>{

    observer.observe(counter);

});







/* ===============================
   SCROLL REVEAL ANIMATION
================================ */


const revealElements = document.querySelectorAll(
".mission-card, .team-card, .feature-card, .testimonial-card, .story-image, .story-content"
);



revealElements.forEach(element=>{

    element.classList.add("reveal");

});




const revealObserver = new IntersectionObserver((entries)=>{


    entries.forEach(entry=>{


        if(entry.isIntersecting){


            entry.target.classList.add("show");


        }


    });


},{

    threshold:0.15

});




revealElements.forEach(element=>{


    revealObserver.observe(element);


});






/* ===============================
   SMOOTH SCROLL
================================ */


document.querySelectorAll('a[href^="#"]').forEach(anchor=>{


    anchor.addEventListener("click",function(e){


        const target = document.querySelector(
            this.getAttribute("href")
        );


        if(target){

            e.preventDefault();


            target.scrollIntoView({

                behavior:"smooth"

            });


        }


    });


});

/* ===============================
   CONTACT FORM
================================ */


const contactForm = document.querySelector("#contactForm");


if(contactForm){


contactForm.addEventListener("submit", async(e)=>{


    e.preventDefault();


    const formData = {

        name: contactForm.name.value,

        phone: contactForm.phone.value,

        email: contactForm.email.value,

        message: contactForm.message.value

    };



    console.log(formData);



    /*
    Later replace this with:

    fetch("http://localhost:5000/api/contact",{
        method:"POST",
        headers:{
            "Content-Type":"application/json"
        },
        body:JSON.stringify(formData)
    })

    */


    alert(
        "Votre message a été envoyé avec succès."
    );


    contactForm.reset();


});


}