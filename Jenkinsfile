pipeline {
    agent any

    stages {

        stage('Build') {
            steps {
                bat 'echo Building the project'
            }
        }

        stage('Test') {
            steps {
                bat 'echo Running tests'
            }
        }

        stage('Deploy') {
            steps {
                bat 'echo Deploying application'
            }
        }
    }

    post {
    success {
        echo 'Pipeline completed successfully'

        emailext(
            subject: "SUCCESS: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
            body: """
Hello,

The Jenkins pipeline completed successfully.

Job: ${env.JOB_NAME}
Build Number: ${env.BUILD_NUMBER}
Status: SUCCESS

Please check Jenkins for more details.
""",
            to: "rohannayak.ca@silveroakuni.ac.in"
        )
    }

    failure {
        echo 'Pipeline failed'

        emailext(
            subject: "FAILURE: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
            body: """
Hello,

The Jenkins pipeline has failed.

Job: ${env.JOB_NAME}
Build Number: ${env.BUILD_NUMBER}
Status: FAILURE

Please check the Jenkins console output.
""",
            to: "rohannayak.ca@silveroakuni.ac.in"
        )
    }
}
}
